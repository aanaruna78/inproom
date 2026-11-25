"""INPROOM-157 (story INPROOM-27): blob store key layout, idempotent writes and verified reads."""

import uuid

import pytest

from app.content import store as blobs


def test_keys_are_content_addressed_under_a_per_tenant_prefix():
    a, b = uuid.uuid4(), uuid.uuid4()
    digest = blobs.sha256(b"same bytes")
    ka, kb = blobs.blob_key(a, digest), blobs.blob_key(b, digest)
    assert ka == f"tenants/{a}/blobs/sha256/{digest[:2]}/{digest}"
    assert ka != kb and ka.startswith(blobs.tenant_prefix(a)) and not kb.startswith(blobs.tenant_prefix(a))
    with pytest.raises(ValueError):
        blobs.blob_key(a, "../" + digest[3:])
    with pytest.raises(TypeError):
        blobs.blob_key(str(a), digest)  # type: ignore[arg-type]


def test_put_is_idempotent_and_reads_are_verified():
    store, tenant = blobs.MemoryStore(), uuid.uuid4()
    digest, key, written = blobs.put(store, tenant, b"hello", "text/plain")
    assert written and blobs.read(store, key) == b"hello"
    assert blobs.put(store, tenant, b"hello", "text/plain") == (digest, key, False)  # a retry writes nothing
    assert store.puts == 1
    store.blobs[key] = (b"tampered", "text/plain", store.blobs[key][2])
    with pytest.raises(blobs.BlobCorrupt):
        blobs.read(store, key)
    assert blobs.put(store, tenant, b"hello", "text/plain")[2]  # a corrupt copy is replaced on the next put
    assert blobs.read(store, key) == b"hello"


def test_missing_and_unavailable_are_distinct_errors():
    store = blobs.MemoryStore()
    with pytest.raises(blobs.BlobMissing):
        blobs.read(store, blobs.blob_key(uuid.uuid4(), blobs.sha256(b"x")))
    store.fail = blobs.ObjectStoreUnavailable("down")
    with pytest.raises(blobs.ObjectStoreUnavailable) as exc:
        blobs.put(store, uuid.uuid4(), b"x", "text/plain")
    assert exc.value.code == "OBJECTSTORE_UNAVAILABLE"
