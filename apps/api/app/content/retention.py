"""Retention purge for stored source content (INPROOM-160 / story INPROOM-27).

A daily `purge` job per project (source_id = project id) applies the project's `sources.retention` policy:
- the CURRENT revision of a live item is never purged, nor are the newest `keep_revisions` revisions;
- an older revision is purged `superseded_days` after the revision that replaced it was fetched;
- every revision of a DELETED item is purged `deleted_days` after the deletion (items whose project source was
  removed are marked DELETED first, which starts that clock).
Purging sets `purged_at` on the revision and its derivatives and keeps all lineage metadata. A blob is
deleted only when no unpurged revision or derivative anywhere in the tenant still references it (blobs are
shared per tenant). Blobs nothing references at all (a sync that rolled back after writing its blob, a
derivative superseded while being built, a delete that failed half-way) are collected by the daily per-tenant
`sweep` job (source_id = tenant id) after a grace period: tenant-level, because such a blob may be the only
trace a tenant has (its very first ingestion failed), so no project-level schedule could reach it.

Concurrency: record_revision and the derive job hold a SHARED per-tenant advisory lock from writing a blob
until their transaction commits; blob deletion holds it EXCLUSIVELY and re-checks references under it. So a
blob that a concurrent sync is about to reuse is never deleted underneath it.
"""

import logging
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import Connection, insert, select, text, update

from app.config import get_settings
from app.content import store as blobs
from app.db import tenant_session
from app.db_schema import (
    derivatives,
    outbox_events,
    project_confluence_sources,
    project_jira_sources,
    project_repositories,
    projects,
    source_objects,
    source_revisions,
)
from app.sync import queue
from app.sync.worker import JobContext, SkipJob, handler

log = logging.getLogger("inproom.content.retention")
AUTO_SCHEDULE = True  # worker.schedule_tick queues due purges; source-queue test suites switch it off
MAX_ORPHANS = 10_000
SOURCE_TABLES = {
    "confluence": project_confluence_sources,
    "jira": project_jira_sources,
    "git": project_repositories,
}


def blob_lock(conn: Connection, tenant: uuid.UUID, *, exclusive: bool = False) -> None:
    """Transaction-scoped advisory lock over the tenant's blob namespace (see the module docstring)."""
    fn = "pg_advisory_xact_lock" if exclusive else "pg_advisory_xact_lock_shared"
    conn.execute(text(f"SELECT {fn}(hashtextextended(:k, 0))"), {"k": f"inproom:blobs:{tenant}"})


def purgeable(
    revisions: list[dict[str, Any]],
    *,
    current_id: uuid.UUID | None,
    deleted_at: datetime | None,
    policy: Any,
    now: datetime,
) -> list[uuid.UUID]:
    """Ids of one item's revisions to purge now. Pure; `revisions` are the item's rows in any order."""
    ordered = sorted(revisions, key=lambda r: r["seq"])
    if deleted_at is not None:
        if deleted_at <= now - timedelta(days=policy.deleted_days):
            return [r["id"] for r in ordered if r["purged_at"] is None]
        return []
    keep = {r["id"] for r in ordered[-policy.keep_revisions :]} | {current_id}
    cutoff = now - timedelta(days=policy.superseded_days)
    out = []
    for older, newer in zip(ordered, ordered[1:], strict=False):
        if older["id"] in keep or older["purged_at"] is not None:
            continue
        if newer["fetched_at"] <= cutoff:  # replaced long enough ago
            out.append(older["id"])
    return out


def _orphan_source_items(conn: Connection, project_id: uuid.UUID, now: datetime) -> int:
    """Items whose project source no longer exists are DELETED (starting the deleted_days clock)."""
    marked = 0
    for kind, table in SOURCE_TABLES.items():
        marked += conn.execute(
            update(source_objects)
            .where(
                source_objects.c.project_id == project_id,
                source_objects.c.source_kind == kind,
                source_objects.c.status != "DELETED",
                source_objects.c.source_id.not_in(select(table.c.id).where(table.c.project_id == project_id)),
            )
            .values(status="DELETED", status_reason="source removed from the project", deleted_at=now)
        ).rowcount
    return marked


def _referenced(conn: Connection, keys: set[str] | None = None) -> set[str]:
    """Blob keys still referenced by an unpurged revision or derivative in this tenant (RLS-scoped)."""
    found: set[str] = set()
    for table in (source_revisions, derivatives):
        stmt = select(table.c.blob_key).where(table.c.purged_at.is_(None), table.c.blob_key.is_not(None))
        if keys is not None:
            stmt = stmt.where(table.c.blob_key.in_(sorted(keys)))
        found |= set(conn.execute(stmt).scalars())
    return found


def _orphan_vectors(conn: Connection) -> int:
    """INPROOM-206: the tenant's vectors whose text no chunk carries any more (deleted, purged, superseded
    content) are deleted with the content. Vectors are deduplicated per tenant, so one still used by any
    project's chunk stays."""
    from sqlalchemy import delete, exists

    from app.db_schema import chunk_embeddings as e
    from app.db_schema import knowledge_chunks as k

    return conn.execute(
        delete(e).where(~exists().where(k.c.tenant_id == e.c.tenant_id, k.c.sha256 == e.c.sha256))
    ).rowcount


def purge_project(tenant: uuid.UUID, project_id: uuid.UUID, store: blobs.BlobStore, now: datetime) -> dict:
    """Mark what the policy releases, then delete the blobs nobody references any more."""
    from app.modules import settings as settings_module

    with tenant_session(tenant) as conn:
        project = conn.execute(select(projects).where(projects.c.id == project_id)).mappings().first()
        if project is None:
            raise SkipJob("project is gone")
        policy = settings_module.current_settings(conn, tenant, dict(project)).settings.sources.retention
        marked_deleted = _orphan_source_items(conn, project_id, now)
        objects = {
            o.id: o
            for o in conn.execute(
                select(
                    source_objects.c.id,
                    source_objects.c.status,
                    source_objects.c.current_revision_id,
                    source_objects.c.deleted_at,
                ).where(source_objects.c.project_id == project_id)
            )
        }
        by_object: dict[uuid.UUID, list[dict[str, Any]]] = {}
        for rev in conn.execute(
            select(
                source_revisions.c.id,
                source_revisions.c.object_id,
                source_revisions.c.seq,
                source_revisions.c.fetched_at,
                source_revisions.c.purged_at,
            ).where(source_revisions.c.object_id.in_(list(objects)))
        ).mappings():
            by_object.setdefault(rev["object_id"], []).append(dict(rev))
        ids: list[uuid.UUID] = []
        for object_id, revs in by_object.items():
            obj = objects[object_id]
            ids += purgeable(
                revs,
                current_id=obj.current_revision_id,
                deleted_at=obj.deleted_at if obj.status == "DELETED" else None,  # only a deletion starts it
                policy=policy,
                now=now,
            )
        keys: set[str] = set()
        if ids:
            keys |= set(
                conn.execute(
                    update(source_revisions)
                    .where(source_revisions.c.id.in_(ids))
                    .values(purged_at=now)
                    .returning(source_revisions.c.blob_key)
                ).scalars()
            )
            purged = conn.execute(
                update(derivatives)
                .where(derivatives.c.revision_id.in_(ids), derivatives.c.purged_at.is_(None))
                .values(purged_at=now)
                .returning(derivatives.c.id, derivatives.c.blob_key)
            ).all()
            keys |= {p.blob_key for p in purged}
            from app.content.code import projection  # INPROOM-175: purged symbols leave the search projection

            projection.drop(conn, [p.id for p in purged])
        keys.discard(None)
        counts = {
            "items_marked_deleted": marked_deleted,
            "revisions_purged": len(ids),
            "vectors_deleted": _orphan_vectors(conn),
        }
    # Released blobs: deleted under the exclusive tenant blob lock, re-checking references at that moment.
    deleted = 0
    with tenant_session(tenant) as conn:
        blob_lock(conn, tenant, exclusive=True)
        for key in sorted(keys - _referenced(conn, keys)):
            store.delete(key)
            deleted += 1
        counts["blobs_deleted"] = deleted
        if any(counts.values()):
            conn.execute(
                insert(outbox_events).values(
                    tenant_id=tenant, type="content.purged", payload={"project_id": str(project_id)} | counts
                )
            )
    log.info("content purge project=%s %s", project_id, counts)
    return counts


def sweep_tenant(tenant: uuid.UUID, store: blobs.BlobStore, now: datetime) -> dict:
    """Delete the tenant's blobs that nothing unpurged references and that are older than the grace period:
    left by a sync that rolled back after writing its blob (even the very first one, when no content row
    exists anywhere), by a derivative superseded while being built, or by a delete that failed half-way.
    Tenant-level because blobs are: a schedule derived from rows could never reach such orphans."""
    grace = timedelta(seconds=get_settings().content_orphan_grace_seconds)
    orphans = 0
    with tenant_session(tenant) as conn:
        blob_lock(conn, tenant, exclusive=True)  # no sync is between writing a blob and committing its row
        live = _referenced(conn)
        for info in store.list(blobs.tenant_prefix(tenant)):
            if orphans >= MAX_ORPHANS:
                break
            if info.key not in live and info.modified <= now - grace:
                store.delete(info.key)
                orphans += 1
        if orphans:
            conn.execute(
                insert(outbox_events).values(
                    tenant_id=tenant, type="content.swept", payload={"orphans_deleted": orphans}
                )
            )
    log.info("content sweep tenant=%s orphans_deleted=%s", tenant, orphans)
    return {"orphans_deleted": orphans, "more": orphans >= MAX_ORPHANS}


@handler("purge")
def purge(ctx: JobContext) -> dict[str, Any]:
    result = purge_project(ctx.tenant, ctx.job["source_id"], blobs.get_store(), datetime.now(UTC))
    with tenant_session(ctx.tenant) as conn:
        queue.succeed(conn, ctx.job, result)
    return result


@handler("sweep")
def sweep(ctx: JobContext) -> dict[str, Any]:
    result = sweep_tenant(ctx.tenant, blobs.get_store(), datetime.now(UTC))
    with tenant_session(ctx.tenant) as conn:
        queue.succeed(conn, ctx.job, result)
    return result
