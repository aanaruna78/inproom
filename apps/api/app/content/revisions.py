"""Source objects and their append-only revision chains (INPROOM-157 / story INPROOM-27).

A source object is one provider item (a Confluence page, a Jira issue, later a file at a path) identified
by (project, source kind, source id, external id). Each distinct content it has had is one revision: seq 1..n,
each pointing at its parent, carrying the content hash, the ACL it was fetched under and the fetching job.

``record_revision``:
1. writes the blob first (content address, idempotent), so a row never points at a missing blob; a blob
   orphaned by a later rollback is invisible and collected by the purge job;
2. upserts and LOCKS the object row, then appends a revision only if the bytes differ from the current one.
   Same bytes => no revision (a re-sync or a concurrent sync of the same content adds nothing). A revert to
   older content is a NEW revision that reuses the stored blob, so lineage stays linear and truthful.
"""

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import Connection, insert, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert

from app.config import get_settings
from app.content import store as blobs
from app.db_schema import outbox_events, source_objects, source_revisions

SOURCE_KINDS = ("confluence", "jira", "git", "upload")
IN_SCOPE = "CURRENT"


@dataclass(frozen=True)
class Recorded:
    object_id: uuid.UUID
    revision_id: uuid.UUID
    seq: int
    content_sha256: str
    created: bool  # False: the bytes equal the current revision, nothing was appended
    status: str


def record_revision(
    conn: Connection,
    store: blobs.BlobStore,
    *,
    tenant: uuid.UUID,
    project_id: uuid.UUID,
    source_kind: str,
    source_id: uuid.UUID,
    external_id: str,
    data: bytes,
    media_type: str,
    title: str = "",
    uri: str | None = None,
    provider_version: str | None = None,
    acl: dict[str, Any] | None = None,
    job_id: uuid.UUID | None = None,
) -> Recorded:
    if source_kind not in SOURCE_KINDS:
        raise ValueError(f"unknown source kind {source_kind!r}")
    acl = acl or {}
    digest = blobs.sha256(data)
    too_large = len(data) > get_settings().content_max_bytes
    key = None
    if not too_large:
        digest, key, _ = blobs.put(store, tenant, data, media_type)  # blob first, row second
    status = "TOO_LARGE" if too_large else IN_SCOPE
    now = datetime.now(UTC)
    ident = {
        "project_id": project_id,
        "source_kind": source_kind,
        "source_id": source_id,
        "external_id": external_id,
    }
    upsert = pg_insert(source_objects).values(tenant_id=tenant, title=title, uri=uri, acl=acl, **ident)
    conn.execute(
        upsert.on_conflict_do_update(
            index_elements=list(ident),
            set_={"last_seen_at": now},  # also takes the row lock for the rest of the transaction
        )
    )
    obj = (
        conn.execute(
            select(source_objects)
            .where(*(source_objects.c[k] == v for k, v in ident.items()))
            .with_for_update()
        )
        .mappings()
        .one()
    )
    current = None
    if obj["current_revision_id"] is not None:
        current = (
            conn.execute(select(source_revisions).where(source_revisions.c.id == obj["current_revision_id"]))
            .mappings()
            .one()
        )
    fields = {"title": title, "uri": uri, "acl": acl, "updated_at": now}
    if current is not None and current["content_sha256"] == digest:
        if obj["status"] != status or obj["deleted_at"] is not None:
            fields |= {"status": status, "status_reason": None, "deleted_at": None}
        changed = {k: v for k, v in fields.items() if k == "updated_at" or obj[k] != v}
        if len(changed) > 1:
            conn.execute(update(source_objects).where(source_objects.c.id == obj["id"]).values(**changed))
        return Recorded(obj["id"], current["id"], current["seq"], digest, False, status)
    seq = obj["revisions"] + 1
    revision = conn.execute(
        insert(source_revisions)
        .values(
            tenant_id=tenant,
            object_id=obj["id"],
            seq=seq,
            parent_revision_id=current["id"] if current else None,
            provider_version=provider_version,
            content_sha256=digest,
            size_bytes=len(data),
            media_type=media_type,
            blob_key=key,
            acl=acl,
            job_id=job_id,
        )
        .returning(source_revisions.c.id)
    ).scalar_one()
    conn.execute(
        update(source_objects)
        .where(source_objects.c.id == obj["id"])
        .values(
            current_revision_id=revision,
            revisions=seq,
            status=status,
            status_reason="larger than the content size limit" if too_large else None,
            deleted_at=None,
            **fields,
        )
    )
    from app.content import derivatives  # late: derivatives -> worker -> handlers -> ingest -> here

    derivatives.on_new_revision(conn, tenant, dict(obj), revision, media_type, stored=key is not None)
    conn.execute(
        insert(outbox_events).values(
            tenant_id=tenant,
            type="source.revision.created",
            payload={
                "project_id": str(project_id),
                "source_kind": source_kind,
                "source_id": str(source_id),
                "external_id": external_id,
                "object_id": str(obj["id"]),
                "revision_id": str(revision),
                "seq": seq,
                "content_sha256": digest,
                "status": status,
            },
        )
    )
    return Recorded(obj["id"], revision, seq, digest, True, status)


def revision_bytes(store: blobs.BlobStore, revision: dict[str, Any]) -> bytes:
    """The revision's original bytes, verified. BlobMissing when it has no stored blob (too large, purged)."""
    if revision.get("blob_key") is None or revision.get("purged_at") is not None:
        raise blobs.BlobMissing("this revision's content is not stored")
    return blobs.read(store, revision["blob_key"])
