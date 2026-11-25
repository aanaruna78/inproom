"""Derivatives linked to canonical revisions; supersede, invalidate and rebuild (INPROOM-159 / INPROOM-27).

Every derivative row names exactly one source revision and one producer@version. Statuses:
- PENDING      waiting for the project source's `derive` job;
- READY        built: output_sha256 / blob_key hold the artefact;
- FAILED       the producer could not build it (error explains why; permanent for this revision);
- SUPERSEDED   its revision is no longer the object's current one;
- INVALIDATED  explicitly invalidated (a fresh PENDING row replaces it).
At most one PENDING/READY/FAILED row exists per (revision, kind, producer, version) - a partial unique index -
so repeated invalidation or scheduling can never duplicate work.

Consumers (retrieval projections, E03-S05, E04) must read through ``live()``: READY derivatives of the CURRENT
revision of an in-scope object whose source is ACTIVE.
"""

import uuid
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import Connection, and_, insert, or_, select, text, update
from sqlalchemy.dialects.postgresql import insert as pg_insert

from app.content import store as blobs
from app.content.producers import PRODUCERS, ProducerError, for_media_type
from app.db import tenant_session
from app.db_schema import (
    derivatives,
    outbox_events,
    project_confluence_sources,
    project_jira_sources,
    projects,
    source_objects,
    source_revisions,
)
from app.sync import queue
from app.sync.worker import JobContext, SkipJob, handler

LIVE = ("PENDING", "READY", "FAILED")
BATCH = 200
_LIVE_WHERE = text("status IN ('PENDING','READY','FAILED')")
SOURCE_TABLES = {"confluence": project_confluence_sources, "jira": project_jira_sources}


def _pending(conn: Connection, tenant, project_id, revision_id, media_type: str, reason: str | None) -> int:
    added = 0
    for producer in for_media_type(media_type):
        stmt = pg_insert(derivatives).values(
            tenant_id=tenant,
            project_id=project_id,
            revision_id=revision_id,
            kind=producer.kind,
            producer=producer.name,
            producer_version=producer.version,
            reason=reason,
        )
        inserted = conn.execute(
            stmt.on_conflict_do_nothing(
                index_elements=["revision_id", "kind", "producer", "producer_version"],
                index_where=_LIVE_WHERE,
            ).returning(derivatives.c.id)  # count rows: rowcount is not reliable with RETURNING
        ).all()
        added += len(inserted)
    return added


def on_new_revision(
    conn: Connection,
    tenant: uuid.UUID,
    obj: dict[str, Any],
    revision_id: uuid.UUID,
    media_type: str,
    stored: bool,
) -> None:
    """Called by record_revision in its transaction: older revisions' derivatives are superseded and the new
    revision's are queued for the source's derive job."""
    others = select(source_revisions.c.id).where(
        source_revisions.c.object_id == obj["id"], source_revisions.c.id != revision_id
    )
    conn.execute(
        update(derivatives)
        .where(derivatives.c.revision_id.in_(others), derivatives.c.status.in_(LIVE))
        .values(status="SUPERSEDED", invalidated_at=datetime.now(UTC), reason="a newer revision exists")
    )
    if stored and _pending(conn, tenant, obj["project_id"], revision_id, media_type, "new revision"):
        queue.enqueue(conn, tenant, obj["project_id"], "derive", obj["source_id"], "schedule")


def invalidate(
    conn: Connection,
    tenant: uuid.UUID,
    project_id: uuid.UUID,
    *,
    kind: str,
    source_id: uuid.UUID,
    external_id: str | None,
    producer: str | None,
    reason: str,
) -> dict[str, Any]:
    """Invalidate the current revisions' derivatives in scope and queue their rebuild. Idempotent: rows
    already PENDING stay as they are; missing ones for registered producers are created."""
    o, r, d = source_objects, source_revisions, derivatives
    scope = [o.c.project_id == project_id, o.c.source_kind == kind, o.c.source_id == source_id]
    if external_id is not None:
        scope.append(o.c.external_id == external_id)
    current = (
        select(r.c.id, r.c.media_type, r.c.blob_key, r.c.purged_at)
        .select_from(r.join(o, o.c.current_revision_id == r.c.id))
        .where(*scope)
    )
    revisions = [dict(x) for x in conn.execute(current).mappings()]
    ids = [x["id"] for x in revisions]
    stale = [d.c.revision_id.in_(ids), d.c.status.in_(("READY", "FAILED"))]
    if producer:
        stale.append(d.c.producer == producer)
    now = datetime.now(UTC)
    invalidated = conn.execute(
        update(d).where(*stale).values(status="INVALIDATED", invalidated_at=now, reason=reason[:300])
    ).rowcount
    queued = 0
    for rev in revisions:
        if rev["blob_key"] is None or rev["purged_at"] is not None:
            continue  # nothing to rebuild from
        queued += _pending(conn, tenant, project_id, rev["id"], rev["media_type"], reason[:300])
    pending = conn.execute(
        select(d.c.id).where(d.c.revision_id.in_(ids), d.c.status == "PENDING", *(stale[2:]))
    ).all()
    job = None
    if pending:
        job = queue.enqueue(conn, tenant, project_id, "derive", source_id, "manual")["id"]
    result = {
        "objects": len(revisions),
        "invalidated": invalidated,
        "queued": queued,
        "pending": len(pending),
        "job_id": str(job) if job else None,
    }
    conn.execute(
        insert(outbox_events).values(
            tenant_id=tenant,
            type="content.derivatives.invalidated",
            payload={
                "project_id": str(project_id),
                "source_kind": kind,
                "source_id": str(source_id),
                "external_id": external_id,
                "producer": producer,
                "reason": reason[:300],
            }
            | {k: v for k, v in result.items() if k != "job_id"},
        )
    )
    return result


def live(conn: Connection, project_id: uuid.UUID, kind: str | None = None) -> list[dict[str, Any]]:
    """READY derivatives that consumers may use: current revision, CURRENT object, ACTIVE source."""
    o, r, d = source_objects, source_revisions, derivatives
    active = [
        and_(o.c.source_kind == k, o.c.source_id.in_(select(t.c.id).where(t.c.status == "ACTIVE")))
        for k, t in SOURCE_TABLES.items()
    ]
    stmt = (
        select(d, o.c.source_kind, o.c.source_id, o.c.external_id, r.c.seq, r.c.content_sha256)
        .select_from(d.join(r, r.c.id == d.c.revision_id).join(o, o.c.current_revision_id == r.c.id))
        .where(
            d.c.project_id == project_id,
            d.c.status == "READY",
            d.c.purged_at.is_(None),
            o.c.status == "CURRENT",
            or_(*active, o.c.source_kind.not_in(list(SOURCE_TABLES))),
        )
        .order_by(o.c.source_kind, o.c.external_id, d.c.producer)
    )
    if kind:
        stmt = stmt.where(d.c.kind == kind)
    return [dict(x) for x in conn.execute(stmt).mappings()]


def _parse_failed(ctx: JobContext, row: dict[str, Any], error: dict[str, Any]) -> None:
    with tenant_session(ctx.tenant) as conn:
        conn.execute(
            insert(outbox_events).values(
                tenant_id=ctx.tenant,
                type="content.parse.failed",
                payload={
                    "project_id": str(ctx.job["project_id"]),
                    "source_kind": row["source_kind"],
                    "source_id": str(ctx.job["source_id"]),
                    "external_id": row["external_id"],
                    "seq": row["seq"],
                    "derivative_id": str(row["id"]),
                    "code": error["code"],
                },
            )
        )


def _finish(tenant, derivative_id, **values) -> bool:
    with tenant_session(tenant) as conn:
        return bool(
            conn.execute(
                update(derivatives)
                .where(derivatives.c.id == derivative_id, derivatives.c.status == "PENDING")
                .values(**values)
            ).rowcount
        )


@handler("derive")
def derive(ctx: JobContext) -> dict[str, Any]:
    """Build the PENDING derivatives of one project source, oldest first, BATCH at a time."""
    tenant = ctx.tenant
    o, r, d = source_objects, source_revisions, derivatives
    with tenant_session(tenant) as conn:
        status = conn.execute(
            select(projects.c.status).where(projects.c.id == ctx.job["project_id"])
        ).scalar()
        if status in (None, "ARCHIVED"):
            raise SkipJob("project is archived or gone")
        rows = [
            dict(x)
            for x in conn.execute(
                select(
                    d.c.id, d.c.producer, d.c.producer_version, r.c.blob_key, r.c.purged_at, r.c.media_type
                )
                .add_columns(r.c.seq, o.c.source_kind, o.c.external_id)
                .select_from(d.join(r, r.c.id == d.c.revision_id).join(o, o.c.id == r.c.object_id))
                .where(o.c.source_id == ctx.job["source_id"], d.c.status == "PENDING")
                .order_by(d.c.created_at, d.c.id)
                .limit(BATCH + 1)
            ).mappings()
        ]
    more, rows = len(rows) > BATCH, rows[:BATCH]
    store = blobs.get_store()
    counts: dict[str, Any] = {"built": 0, "failed": 0, "skipped": 0, "failures": {}}
    for row in rows:
        producer = PRODUCERS.get(row["producer"])
        error = None
        if producer is None or producer.version != row["producer_version"]:
            error = {"code": "UNKNOWN_PRODUCER", "message": f"{row['producer']}@{row['producer_version']}"}
        elif row["blob_key"] is None or row["purged_at"] is not None:
            error = {"code": "NOT_STORED", "message": "the revision's bytes are not stored"}
        if error is None:
            try:
                data = blobs.read(store, row["blob_key"])
                output = producer.build(data, row["media_type"])
            except (blobs.BlobMissing, blobs.BlobCorrupt, ProducerError) as exc:
                error = {"code": exc.code, "message": str(exc)[:300]}
        if error is not None:
            done = _finish(tenant, row["id"], status="FAILED", error=error, built_at=datetime.now(UTC))
            counts["failed" if done else "skipped"] += 1
            if done:
                counts["failures"][error["code"]] = counts["failures"].get(error["code"], 0) + 1
                if row["producer"] == "parse":  # INPROOM-167: parse failures are visible downstream
                    _parse_failed(ctx, row, error)
            continue
        with tenant_session(tenant) as conn:
            from app.content.retention import blob_lock

            blob_lock(conn, tenant)  # shared: the output blob cannot be purged before this commits
            digest, key, _ = blobs.put(store, tenant, output, producer.output_media_type)  # outage: retries
            done = bool(
                conn.execute(
                    update(derivatives)
                    .where(derivatives.c.id == row["id"], derivatives.c.status == "PENDING")
                    .values(
                        status="READY",
                        output_sha256=digest,
                        size_bytes=len(output),
                        blob_key=key,
                        error=None,
                        built_at=datetime.now(UTC),
                    )
                ).rowcount
            )
        counts["built" if done else "skipped"] += 1  # skipped: superseded or invalidated meanwhile
    with tenant_session(tenant) as conn:
        if more:
            queue.enqueue(
                conn, tenant, ctx.job["project_id"], "derive", ctx.job["source_id"], ctx.job["reason"]
            )
        result = counts | {"more": more}
        queue.succeed(conn, ctx.job, result)
    return result
