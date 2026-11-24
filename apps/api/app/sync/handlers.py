"""Per-source-kind sync handlers (INPROOM-141/142).

Each handler reads provider state OUTSIDE any transaction, then applies its changes, its sync state and its
job completion in ONE tenant transaction under the project lock (queue.succeed re-checks the lease), so
effects are committed exactly once even if the job is re-run after a crash.
"""

from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import insert, select

from app.connectors import vault
from app.connectors.base import NotFoundOrNoAccess
from app.connectors.registry import confluence_for, jira_for, provider_for
from app.db import tenant_session
from app.db_schema import outbox_events, project_repositories, projects, source_connections
from app.modules import jira_schema, jira_sources
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


# ---------------------------------------------------------------- GitHub (INPROOM-142)


def _event(conn, ctx: JobContext, kind: str, payload: dict[str, Any]) -> None:
    conn.execute(
        insert(outbox_events).values(
            tenant_id=ctx.tenant,
            type=kind,
            payload=payload | {"reason": ctx.job["reason"], "job_id": str(ctx.job["id"])},
        )
    )


@handler("git")
def git(ctx: JobContext) -> dict[str, Any]:
    """Watermark = {"heads": {branch: sha}}. A head that differs from the watermark is one change event;
    the first run records a baseline (before_sha null). Missing branches are reported, not emitted."""
    with tenant_session(ctx.tenant) as conn:
        repo = (
            conn.execute(
                select(project_repositories).where(project_repositories.c.id == ctx.job["source_id"])
            )
            .mappings()
            .first()
        )
        if repo is None:
            raise SkipJob("repository no longer selected")
        if repo["status"] != "ACTIVE":
            raise SkipJob(f"repository is paused: {repo['status_reason']}")
        row, token = load_connection_and_token(conn, ctx.tenant, repo["connection_id"])
    provider = provider_for(row["provider"], row["base_url"], token, ctx.transports)
    if ctx.job["mode"] == "full" and not provider.get_repository(repo["provider_repo_id"]).can_read:
        raise NotFoundOrNoAccess("the credential can no longer read this repository")
    heads = {b: provider.branch_head(repo["provider_repo_id"], b) for b in repo["branches"]}
    with tenant_session(ctx.tenant) as conn:
        lock_project(conn, ctx, "git")
        known = queue.watermark(conn, ctx.job).get("heads", {})
        changed = []
        for branch, sha in sorted(heads.items()):
            if sha is None or known.get(branch) == sha:
                continue
            changed.append(branch)
            _event(
                conn,
                ctx,
                "source.repository.changed",
                {
                    "project_id": str(repo["project_id"]),
                    "connection_id": str(repo["connection_id"]),
                    "repository": repo["full_name"],
                    "provider_repo_id": repo["provider_repo_id"],
                    "branch": branch,
                    "before_sha": known.get(branch),
                    "after_sha": sha,
                },
            )
        missing = sorted(b for b, sha in heads.items() if sha is None)
        result = {"changed_branches": changed, "missing_branches": missing}
        queue.succeed(conn, ctx.job, result, {"heads": {b: s for b, s in heads.items() if s}})
    return result


# ---------------------------------------------------------------- Jira (INPROOM-142)

JIRA_MAX_IDS = 50_000
JIRA_MAX_SEARCH_PAGES = 1_000  # 50 issues per page


def _ts(value: str) -> datetime:
    """Jira timestamps look like 2026-11-22T09:00:00.000+0000."""
    return datetime.strptime(value, "%Y-%m-%dT%H:%M:%S.%f%z")


def plan_jira(
    known: dict[str, Any], issues: list[dict[str, Any]], full: bool
) -> tuple[list[dict], list[str], dict[str, Any]]:
    """(changed issues, deleted issue ids, new watermark). Pure: unit-tested.

    Watermark = {"updated": iso of the newest change seen, "seen": ids updated exactly then, "ids": all ids
    (full runs only)}. Jira's `updated >=` query is minute-granular, so we re-read a small overlap and drop
    what we already emitted: a change is newer than the watermark, or equally new but not yet seen.
    An out-of-order (older) update therefore never re-emits."""
    mark = _ts(known["updated"]) if known.get("updated") else None
    seen = set(known.get("seen", []))
    changed = []
    for issue in issues:
        updated = (issue.get("fields") or {}).get("updated")
        if not updated:
            continue
        when = _ts(updated)
        if mark is None or when > mark or (when == mark and str(issue["id"]) not in seen):
            changed.append(issue)
    times = [
        (_ts(i["fields"]["updated"]), str(i["id"])) for i in issues if (i.get("fields") or {}).get("updated")
    ]
    newest = max((t for t, _ in times), default=mark)
    new_seen = {i for t, i in times if t == newest} | (seen if newest == mark else set())
    watermark: dict[str, Any] = {
        "updated": newest.strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + newest.strftime("%z") if newest else None,
        "seen": sorted(new_seen),
        "ids": known.get("ids"),
    }
    deleted: list[str] = []
    if full:
        current = sorted({str(i["id"]) for i in issues})
        if known.get("ids") is not None:
            deleted = sorted(set(known["ids"]) - set(current))
        watermark["ids"] = current if len(current) <= JIRA_MAX_IDS else None
    return changed, deleted, watermark


@handler("jira")
def jira(ctx: JobContext) -> dict[str, Any]:
    """Incremental: issues updated since the watermark. Full (daily or on a deletion hint): the project's
    whole issue id set too (deletions) and schema rediscovery (schema.changed only if it changed)."""
    with tenant_session(ctx.tenant) as conn:
        found = jira_sources._sources(conn, ctx.tenant, id=ctx.job["source_id"])
        if not found:
            raise SkipJob("Jira project no longer selected")
        source = found[0]
        if source["status"] != "ACTIVE":
            raise SkipJob(f"Jira project is paused: {source['status_reason']}")
        row, token = load_connection_and_token(conn, ctx.tenant, source["connection_id"])
        known = queue.watermark(conn, ctx.job)
    provider = jira_for(row, token, ctx.transports)
    full = ctx.job["mode"] == "full" or known.get("ids") is None
    jql = f'project = "{source["jira_project_id"]}"'
    if not full and known.get("updated"):
        since = _ts(known["updated"]) - timedelta(minutes=1)
        jql += f' AND updated >= "{since.strftime("%Y-%m-%d %H:%M")}"'
    issues = list(
        provider.iter_issues(jql + " ORDER BY updated ASC", ["updated"], limit=JIRA_MAX_SEARCH_PAGES)
    )
    schema = capabilities = None
    if full:
        schema, capabilities = jira_schema.discover(provider, source["jira_project_id"])
    with tenant_session(ctx.tenant) as conn:
        write_mode = lock_project(conn, ctx, "jira")
        changed, deleted, watermark = plan_jira(known, issues, full)
        base = {
            "project_id": str(source["project_id"]),
            "source_id": str(source["id"]),
            "jira_key": source["jira_key"],
        }
        for issue in changed:
            _event(
                conn,
                ctx,
                "source.jira.issue.changed",
                base
                | {"issue_id": str(issue["id"]), "key": issue["key"], "updated": issue["fields"]["updated"]},
            )
        for issue_id in deleted:
            _event(conn, ctx, "source.jira.issue.deleted", base | {"issue_id": issue_id})
        schema_changed = False
        if schema is not None:
            current = jira_sources._sources(conn, ctx.tenant, id=source["id"])[0]
            schema_changed, diff = jira_sources._apply_discovery(
                conn, ctx.principal, current, schema, capabilities, write_mode
            )
            if schema_changed:
                jira_sources._emit(
                    conn,
                    ctx.principal,
                    ctx.trace_id,
                    "source.jira.discovered",
                    current,
                    {"changed": True, "diff": diff},
                    "source.jira.schema.changed",
                )
        result = {
            "changed": len(changed),
            "deleted": len(deleted),
            "schema_changed": schema_changed,
            "full": full,
            "ids_tracked": watermark["ids"] is not None,
        }
        queue.succeed(conn, ctx.job, result, watermark)
    return result
