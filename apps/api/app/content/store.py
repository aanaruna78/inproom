"""Content-addressed blob store for original source bytes and derivatives (INPROOM-157 / story INPROOM-27).

Layout: ``tenants/{tenant}/blobs/sha256/{h[0:2]}/{h}``. Identical bytes are stored once per tenant and are
never shared across tenants, so the store offers no cross-tenant "does this hash exist" side channel. A PUT
to the same key with the same bytes is idempotent, so retries cannot duplicate anything.

Only the S3 API is used (SeaweedFS locally, MinIO or S3 in deployments). ``MemoryStore`` is for unit tests.
Every read is verified against the sha256 in its key.
"""

import hashlib
import threading
import uuid
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import UTC, datetime
from functools import lru_cache
from typing import Protocol

from app.config import get_settings
from app.connectors.base import ProviderError

HASH_LEN = 64


class ObjectStoreUnavailable(ProviderError):
    """The object store could not be reached or failed; transient, the sync worker retries."""

    code = "OBJECTSTORE_UNAVAILABLE"


class BlobMissing(ProviderError):
    code = "BLOB_MISSING"


class BlobCorrupt(ProviderError):
    code = "BLOB_CORRUPT"


@dataclass(frozen=True)
class BlobInfo:
    key: str
    size: int
    modified: datetime


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def tenant_prefix(tenant: uuid.UUID) -> str:
    if not isinstance(tenant, uuid.UUID):
        raise TypeError("tenant must be a uuid.UUID")
    return f"tenants/{tenant}/blobs/"


def blob_key(tenant: uuid.UUID, digest: str) -> str:
    if len(digest) != HASH_LEN or any(c not in "0123456789abcdef" for c in digest):
        raise ValueError("digest must be a lowercase hex sha256")
    return f"{tenant_prefix(tenant)}sha256/{digest[:2]}/{digest}"


def digest_of(key: str) -> str:
    return key.rsplit("/", 1)[-1]


class BlobStore(Protocol):
    def put_if_absent(self, key: str, data: bytes, media_type: str) -> bool:
        """Store data under key unless an intact copy is already there. True if it wrote."""

    def get(self, key: str) -> bytes: ...

    def delete(self, key: str) -> None: ...

    def list(self, prefix: str) -> Iterator[BlobInfo]: ...


def put(store: BlobStore, tenant: uuid.UUID, data: bytes, media_type: str) -> tuple[str, str, bool]:
    """(sha256, key, written) for data stored under its content address in the tenant's prefix."""
    digest = sha256(data)
    key = blob_key(tenant, digest)
    return digest, key, store.put_if_absent(key, data, media_type)


def read(store: BlobStore, key: str) -> bytes:
    """The blob's bytes, verified against the sha256 its key names."""
    data = store.get(key)
    if sha256(data) != digest_of(key):
        raise BlobCorrupt(f"stored bytes for {digest_of(key)[:12]} do not match their hash")
    return data


class MemoryStore:
    def __init__(self) -> None:
        self.blobs: dict[str, tuple[bytes, str, datetime]] = {}
        self.fail: Exception | None = None  # tests: make every call raise this
        self.puts = 0
        self._lock = threading.Lock()

    def _check(self) -> None:
        if self.fail is not None:
            raise self.fail

    def put_if_absent(self, key: str, data: bytes, media_type: str) -> bool:
        self._check()
        with self._lock:
            have = self.blobs.get(key)
            if have is not None and sha256(have[0]) == digest_of(key):
                return False
            self.blobs[key] = (bytes(data), media_type, datetime.now(UTC))
            self.puts += 1
            return True

    def get(self, key: str) -> bytes:
        self._check()
        try:
            return self.blobs[key][0]
        except KeyError:
            raise BlobMissing(f"blob {digest_of(key)[:12]} is not in the store") from None

    def delete(self, key: str) -> None:
        self._check()
        self.blobs.pop(key, None)

    def list(self, prefix: str) -> Iterator[BlobInfo]:
        self._check()
        for key, (data, _, at) in sorted(self.blobs.items()):
            if key.startswith(prefix):
                yield BlobInfo(key, len(data), at)


class S3Store:
    """S3 API via boto3. Errors become ObjectStoreUnavailable (transient) or BlobMissing."""

    def __init__(self, url: str, bucket: str, region: str, access_key, secret_key, timeout_s: int) -> None:
        import boto3
        from botocore.config import Config

        self.bucket = bucket
        self.client = boto3.client(
            "s3",
            endpoint_url=url,
            region_name=region,
            aws_access_key_id=access_key or "inproom",
            aws_secret_access_key=secret_key or "inproom",
            config=Config(
                connect_timeout=timeout_s,
                read_timeout=timeout_s * 4,
                retries={"max_attempts": 2, "mode": "standard"},
                s3={"addressing_style": "path"},
                signature_version="s3v4",
            ),
        )
        self._bucket_ready = False
        self._lock = threading.Lock()

    def _call(self, fn, **kwargs):
        from botocore.exceptions import BotoCoreError, ClientError

        try:
            return fn(Bucket=self.bucket, **kwargs)
        except ClientError as exc:
            status = exc.response.get("ResponseMetadata", {}).get("HTTPStatusCode")
            code = exc.response.get("Error", {}).get("Code")
            if status == 404 or code in ("NoSuchKey", "404", "NotFound"):
                raise BlobMissing("blob is not in the store") from None
            raise ObjectStoreUnavailable(f"object store error {status} {code}") from None
        except BotoCoreError as exc:
            raise ObjectStoreUnavailable(f"object store unreachable: {type(exc).__name__}") from None

    def _ensure_bucket(self) -> None:
        if self._bucket_ready:
            return
        with self._lock:
            if self._bucket_ready:
                return
            try:
                self._call(self.client.head_bucket)
            except BlobMissing:
                from botocore.exceptions import ClientError

                try:
                    self.client.create_bucket(Bucket=self.bucket)
                except ClientError as exc:  # created concurrently by another process
                    if exc.response.get("Error", {}).get("Code") not in (
                        "BucketAlreadyOwnedByYou",
                        "BucketAlreadyExists",
                    ):
                        raise ObjectStoreUnavailable("could not create the content bucket") from None
            self._bucket_ready = True

    def put_if_absent(self, key: str, data: bytes, media_type: str) -> bool:
        self._ensure_bucket()
        digest = digest_of(key)
        try:
            head = self._call(self.client.head_object, Key=key)
            meta = head.get("Metadata") or {}
            if meta.get("sha256") == digest and head.get("ContentLength") == len(data):
                return False
        except BlobMissing:
            pass
        self._call(
            self.client.put_object,
            Key=key,
            Body=data,
            ContentType=media_type,
            Metadata={"sha256": digest},
        )
        return True

    def get(self, key: str) -> bytes:
        self._ensure_bucket()
        return self._call(self.client.get_object, Key=key)["Body"].read()

    def delete(self, key: str) -> None:
        self._ensure_bucket()
        self._call(self.client.delete_object, Key=key)

    def list(self, prefix: str) -> Iterator[BlobInfo]:
        self._ensure_bucket()
        token = None
        while True:
            kwargs = {"Prefix": prefix, "MaxKeys": 1000} | ({"ContinuationToken": token} if token else {})
            page = self._call(self.client.list_objects_v2, **kwargs)
            for item in page.get("Contents") or []:
                yield BlobInfo(item["Key"], item["Size"], item["LastModified"])
            if not page.get("IsTruncated"):
                return
            token = page["NextContinuationToken"]


@lru_cache
def get_store() -> BlobStore:
    s = get_settings()
    if s.objectstore_backend == "memory":
        return MemoryStore()
    if s.objectstore_backend != "s3":
        raise ValueError(f"unknown objectstore_backend {s.objectstore_backend!r}")
    return S3Store(
        s.objectstore_url,
        s.objectstore_bucket,
        s.objectstore_region,
        s.objectstore_access_key,
        s.objectstore_secret_key,
        s.objectstore_timeout_s,
    )
