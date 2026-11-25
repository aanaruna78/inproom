"""Resumable Jira inventory scan for migration projects (INPROOM-151 / story INPROOM-19).

Runs as a worker job (sync_jobs kind 'migration', source_id = plan id). Each page of issues is upserted
by Jira issue id IN THE SAME TRANSACTION that advances the run's cursor, so a worker that dies mid-scan is
resumed by the INPROOM-25 lease recovery from the last committed page; overlap is harmless (upserts).
Only a COMPLETED scan over the full history decides what was removed; an interrupted run removes nothing.
"""

from datetime import UTC, datetime
from typing import Any

from sqlalchemy import insert, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert

from app.connectors.base import CredentialInvalid, NotFoundOrNoAccess
from app.connectors.registry import jira_for
from app.db import tenant_session
from app.db_schema import (
    migration_issues,
    migration_plans,
    migration_runs,
    outbox_events,
    project_jira_sources,
    projects,
)
from app.migration.inventory import FIELDS, MAX_ISSUES, diagnose, normalise
from app.modules.migration import supersede_runs
from app.sync import queue
from app.sync.worker import JobContext, PermanentFailure, SkipJob, handler

PAGE = 100
DONE = "<done>"  # cursor sentinel: every page is in, only the final pass remains


def _active_run(conn, plan) -> dict[str, Any]:
    row = (
        conn.execute(
            select(migration_runs)
            .where(
                migration_runs.c.plan_id == plan["id"],
                migration_runs.c.status.in_(("QUEUED", "RUNNING")),
                migration_runs.c.plan_version == plan["version"],  # INPROOM-155: only the current revision
            )
            .order_by(migration_runs.c.created_at.desc())
            .limit(1)
        )
        .mappings()
        .first()
    )
    return dict(row) if row else {}


@handler("migration")
def migration_scan(ctx: JobContext) -> dict[str, Any]:
    tenant = ctx.tenant
    with tenant_session(tenant) as conn:
        plan = (
            conn.execute(select(migration_plans).where(migration_plans.c.id == ctx.job["source_id"]))
            .mappings()
            .first()
        )
        if plan is None:
            raise SkipJob("migration plan gone")
        project = conn.execute(select(projects).where(projects.c.id == plan["project_id"])).mappings().one()
        if project["status"] == "ARCHIVED":
            raise SkipJob("project is archived")
        source = (
            conn.execute(
                select(project_jira_sources).where(project_jira_sources.c.id == plan["jira_source_id"])
            )
            .mappings()
            .first()
        )
        if source is None or source["status"] != "ACTIVE":
            raise SkipJob("the plan's Jira source is not active")
        supersede_runs(conn, dict(plan))  # a run for an older plan revision never completes
        run = _active_run(conn, plan)
        if not run:
            run = dict(
                conn.execute(
                    insert(migration_runs)
                    .values(
                        tenant_id=tenant,
                        project_id=plan["project_id"],
                        plan_id=plan["id"],
                        plan_version=plan["version"],
                    )
                    .returning(*migration_runs.c)
                )
                .mappings()
                .one()
            )
        from app.sync.handlers import load_connection_and_token  # handlers -> worker -> here: import late

        row, token = load_connection_and_token(conn, tenant, source["connection_id"])
    provider = jira_for(row, token, ctx.transports)
    try:
        return _scan(ctx, dict(plan), dict(source), run, row, provider)
    except (CredentialInvalid, NotFoundOrNoAccess, PermanentFailure) as exc:
        _fail(tenant, run["id"], getattr(exc, "code", type(exc).__name__), str(exc))
        raise
    except Exception as exc:  # transient: the worker retries; the run keeps its cursor and resumes
        _note(tenant, run["id"], getattr(exc, "code", "PROVIDER_ERROR"), str(exc))
        raise


def _fail(tenant, run_id, code: str, message: str) -> None:
    with tenant_session(tenant) as conn:
        conn.execute(
            update(migration_runs)
            .where(migration_runs.c.id == run_id)
            .values(
                status="FAILED", finished_at=datetime.now(UTC), error={"code": code, "message": message[:300]}
            )
        )


def _note(tenant, run_id, code: str, message: str) -> None:
    with tenant_session(tenant) as conn:
        conn.execute(
            update(migration_runs)
            .where(migration_runs.c.id == run_id)
            .values(error={"code": code, "message": message[:300], "retrying": True})
        )


def _scan(ctx: JobContext, plan, source, run, connection, provider) -> dict[str, Any]:
    tenant = ctx.tenant
    if run["status"] == "QUEUED":
        permissions = provider.my_permissions(source["jira_key"])
        with tenant_session(tenant) as conn:
            conn.execute(
                update(migration_runs)
                .where(migration_runs.c.id == run["id"])
                .values(
                    status="RUNNING",
                    started_at=datetime.now(UTC),
                    scanned_as={"account": connection["account_login"], "permissions": permissions},
                )
            )
    jql = f'project = "{source["jira_project_id"]}"'
    if plan["history_since"]:
        jql += f' AND updated >= "{plan["history_since"].isoformat()}"'
    jql += " ORDER BY created ASC"
    cursor, seen = run.get("cursor"), run.get("issues_seen", 0)
    while cursor != DONE:
        page = provider.search_issues(jql, FIELDS, cursor, PAGE)
        rows = [normalise(i, source["mapping"]) for i in page.issues]
        seen += len(rows)
        if seen > MAX_ISSUES:
            raise PermanentFailure(
                "TOO_MANY_ISSUES",
                f"more than {MAX_ISSUES} issues: narrow the plan with 'issues updated since'",
            )
        cursor = page.next_page_token or DONE
        with tenant_session(tenant) as conn:  # the page and the checkpoint commit together
            for r in rows:
                stmt = pg_insert(migration_issues).values(
                    tenant_id=tenant,
                    project_id=plan["project_id"],
                    original_key=r["key"],
                    first_seen_run=run["id"],
                    last_seen_run=run["id"],
                    present=True,
                    **r,
                )
                conn.execute(
                    stmt.on_conflict_do_update(
                        index_elements=[migration_issues.c.project_id, migration_issues.c.jira_issue_id],
                        set_={k: stmt.excluded[k] for k in r if k != "jira_issue_id"}
                        | {"last_seen_run": run["id"], "present": True},
                    )
                )
            conn.execute(
                update(migration_runs)
                .where(migration_runs.c.id == run["id"])
                .values(
                    cursor=cursor, pages_done=migration_runs.c.pages_done + 1, issues_seen=seen, error=None
                )
            )
    return _finish(ctx, plan, run)


def _finish(ctx: JobContext, plan, run) -> dict[str, Any]:
    tenant = ctx.tenant
    with tenant_session(tenant) as conn:
        removed = 0
        if plan["history_since"] is None:  # only a complete, full-history scan can say what disappeared
            removed = conn.execute(
                update(migration_issues)
                .where(
                    migration_issues.c.project_id == plan["project_id"],
                    migration_issues.c.present.is_(True),
                    migration_issues.c.last_seen_run != run["id"],
                )
                .values(present=False)
            ).rowcount
        rows = [
            dict(r)
            for r in conn.execute(
                select(migration_issues).where(
                    migration_issues.c.project_id == plan["project_id"], migration_issues.c.present.is_(True)
                )
            ).mappings()
        ]
        flags, counts, diagnostics = diagnose(rows)
        for r in rows:
            wanted = flags.get(r["jira_issue_id"], [])
            if r["flags"] != wanted:
                conn.execute(
                    update(migration_issues)
                    .where(
                        migration_issues.c.project_id == plan["project_id"],
                        migration_issues.c.jira_issue_id == r["jira_issue_id"],
                    )
                    .values(flags=wanted)
                )
        counts["removed"] = conn.execute(
            select(migration_issues.c.jira_issue_id).where(
                migration_issues.c.project_id == plan["project_id"], migration_issues.c.present.is_(False)
            )
        ).rowcount
        counts["removed_this_run"] = removed
        now = datetime.now(UTC)
        conn.execute(
            update(migration_runs)
            .where(migration_runs.c.id == run["id"])
            .values(status="SUCCEEDED", finished_at=now, counts=counts, diagnostics=diagnostics, error=None)
        )
        if plan["status"] == "DRAFT":
            conn.execute(
                update(migration_plans).where(migration_plans.c.id == plan["id"]).values(status="SCANNED")
            )
        payload = {"project_id": str(plan["project_id"]), "run_id": str(run["id"]), "counts": counts}
        conn.execute(
            insert(outbox_events).values(tenant_id=tenant, type="migration.scanned", payload=payload)
        )
        result = {"run_id": str(run["id"]), "issues": counts["issues"], "removed": removed}
        queue.succeed(conn, ctx.job, result)
    return result
