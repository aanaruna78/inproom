"""Per-source-kind sync handlers (INPROOM-141/142).

Each handler reads provider state OUTSIDE any transaction, then applies its changes, its sync state and its
job completion in ONE tenant transaction under the project lock (queue.succeed re-checks the lease), so
effects are committed exactly once even if the job is re-run after a crash.
"""

from typing import Any

from sqlalchemy import select

from app.connectors import vault
from app.connectors.registry import confluence_for
from app.db import tenant_session
from app.db_schema import projects, source_connections
from app.modules import settings as settings_module
from app.modules.confluence_sources import apply_sync, sources
from app.sync import queue
from app.sync.worker import JobContext, PermanentFailure, SkipJob, handler

USABLE = ("ACTIVE", "DEGRADED")


def load_connection_and_token(conn, tenant, connection_id) -> tuple[dict[str, Any], Any]:
    row = (
        conn.execute(
            select(source_connections).where(
                source_connections.c.tenant_id == tenant, source_connections.c.id == connection_id
            )
        )
        .mappings()
        .first()
    )
    if row is None or row["status"] not in USABLE:
        raise SkipJob(f"connection is {row['status'] if row else 'gone'}")
    token = vault.load(conn, tenant, connection_id, "credential")
    if token is None:
        raise SkipJob("connection has no stored credential")
    return dict(row), token


def lock_project(conn, ctx: JobContext, kind: str) -> str:
    """Lock the project like the API routes do; skip archived projects / disallowed kinds. -> write_mode."""
    project = (
        conn.execute(select(projects).where(projects.c.id == ctx.job["project_id"]).with_for_update())
        .mappings()
        .first()
    )
    if project is None or project["status"] == "ARCHIVED":
        raise SkipJob("project is archived or gone")
    current = settings_module.current_settings(conn, ctx.tenant, dict(project)).settings
    if kind not in current.sources.allowed_kinds:
        raise SkipJob(f"project no longer allows {kind} sources")
    return current.write_mode


@handler("confluence")
def confluence(ctx: JobContext) -> dict[str, Any]:
    """Reconcile = the idempotent catalog sync (page changed / deleted / out-of-scope events once)."""
    with tenant_session(ctx.tenant) as conn:
        found = sources(conn, ctx.tenant, id=ctx.job["source_id"])
        if not found:
            raise SkipJob("space no longer selected")
        source = found[0]
        if source["status"] != "ACTIVE":
            raise SkipJob(f"space is paused: {source['status_reason']}")
        row, token = load_connection_and_token(conn, ctx.tenant, source["connection_id"])
    provider = confluence_for(row, token, ctx.transports)
    space = provider.get_space(source["space_key"])
    if space.id != source["space_id"]:
        raise PermanentFailure("SPACE_CHANGED", "the space key now points at a different space; reselect it")
    listing = provider.iter_pages(space)
    with tenant_session(ctx.tenant) as conn:
        write_mode = lock_project(conn, ctx, "confluence")
        found = sources(conn, ctx.tenant, id=ctx.job["source_id"])
        if not found:
            raise SkipJob("space no longer selected")
        counts, changed = apply_sync(conn, ctx.principal, ctx.trace_id, found[0], space, listing, write_mode)
        result = {"counts": counts, "changed": changed, "truncated": listing.truncated}
        queue.succeed(conn, ctx.job, result)
    return result
