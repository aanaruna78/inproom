"""Connection health: attention, sources across projects, disconnect impact, reauthorize follow-up and
adoption (INPROOM-146 / story INPROOM-26).

Recovery rules (see pause_cause, INPROOM-145):
- reauthorizing a connection runs a health check, which resumes sources paused for `access` that now pass;
  each resumed source gets a reconcile job. Sources a person excluded are never touched.
- a revoked connection is never revived. A new connection of the same provider and site can ADOPT its
  sources; each is re-verified with the new credential first. Verified ones resume (with a reconcile); the
  others move over but stay paused for `access` with the reason, so a later fix + check can resume them.
All of these list project names across the tenant, so they need tenant.connection.manage (tenant admins).
"""

import uuid
from datetime import UTC, datetime
from typing import Annotated, Any

from fastapi import APIRouter, Body, Request
from pydantic import BaseModel
from sqlalchemy import Connection, insert, select, update

from app import audit
from app.api_errors import conflict
from app.auth import CurrentPrincipal, Principal
from app.authz import require_tenant
from app.connectors.base import NotFoundOrNoAccess
from app.connectors.jira import READ_PERMISSION
from app.connectors.registry import Transports, confluence_for, jira_for, provider_errors, provider_for
from app.db import tenant_session
from app.db_schema import (
    connection_secrets,
    outbox_events,
    project_confluence_sources,
    project_jira_sources,
    project_repositories,
    projects,
    source_connections,
    source_sync_state,
    sync_jobs,
)
from app.modules.connections import credential_of, load_connection
from app.sync import queue
from app.sync.errors import explain
from app.sync.freshness import freshness

router = APIRouter(tags=["connections"])
PROBLEM = {"description": "problem+json"}
TABLES = {
    "git": (project_repositories, "full_name"),
    "jira": (project_jira_sources, "jira_key"),
    "confluence": (project_confluence_sources, "space_key"),
}
PROVIDER_KIND = {"github": "git", "jira": "jira", "confluence": "confluence"}


class ConnectionSource(BaseModel):
    kind: str
    source_id: uuid.UUID
    name: str
    project_id: uuid.UUID
    project_key: str
    project_name: str
    status: str
    status_reason: str | None
    pause_cause: str | None
    freshness: str
    data_as_of: datetime | None
    problem: dict[str, Any] | None = None


class Impact(BaseModel):
    connection_id: uuid.UUID
    display_name: str
    status: str
    projects: int
    sources: list[ConnectionSource]
    webhooks: dict[str, int]
    summary: str


class AttentionItem(BaseModel):
    connection_id: uuid.UUID
    display_name: str
    provider: str
    status: str
    problems: list[dict[str, Any]]
    affected_sources: int


class AdoptRequest(BaseModel):
    from_connection_id: uuid.UUID


class AdoptResult(BaseModel):
    adopted: list[ConnectionSource]
    still_blocked: list[ConnectionSource]
    kept_excluded: list[ConnectionSource]
    skipped: list[dict[str, Any]]


# ---------------------------------------------------------------- helpers


def connection_sources(conn: Connection, tenant_id: uuid.UUID, row: dict[str, Any]) -> list[dict[str, Any]]:
    """Every source using this connection, across projects, with freshness."""
    kind = PROVIDER_KIND[row["provider"]]
    table, name = TABLES[kind]
    rows = conn.execute(
        select(
            table.c.id,
            table.c[name].label("name"),
            table.c.project_id,
            table.c.status,
            table.c.status_reason,
            table.c.pause_cause,
            projects.c.key.label("project_key"),
            projects.c.name.label("project_name"),
        )
        .join(projects, (projects.c.tenant_id == table.c.tenant_id) & (projects.c.id == table.c.project_id))
        .where(table.c.tenant_id == tenant_id, table.c.connection_id == row["id"])
        .order_by(projects.c.key, table.c[name])
    ).mappings()
    states = {
        r["source_id"]: dict(r)
        for r in conn.execute(
            select(source_sync_state).where(source_sync_state.c.source_kind == kind)
        ).mappings()
    }
    out = []
    for r in rows:
        state = states.get(r["id"])
        fresh = freshness(r["status"], r["pause_cause"], row["status"], state)
        problem = None
        if fresh in ("failing", "paused", "stale"):
            code = ((state or {}).get("last_error") or {}).get("code") if fresh == "failing" else None
            if fresh == "paused":
                code = {"connection_revoked": "REVOKED"}.get(r["pause_cause"]) or (
                    "CREDENTIAL_INVALID" if row["status"] == "INVALID" else "SOURCE_NOT_ACCESSIBLE"
                )
            problem = explain(code or "STALE")
        out.append(
            {
                "kind": kind,
                "source_id": r["id"],
                "name": r["name"],
                "project_id": r["project_id"],
                "project_key": r["project_key"],
                "project_name": r["project_name"],
                "status": r["status"],
                "status_reason": r["status_reason"],
                "pause_cause": r["pause_cause"],
                "freshness": fresh,
                "data_as_of": (state or {}).get("last_success_at"),
                "problem": problem,
            }
        )
    return out


def _connection(conn, principal, request, connection_id, permission="tenant.connection.manage", lock=False):
    return load_connection(conn, principal, request, connection_id, permission, lock=lock)


def _emit(conn, principal: Principal, trace, action: str, row: dict, payload: dict) -> None:
    payload = {"connection_id": str(row["id"]), "provider": row["provider"]} | payload
    audit.record(conn, principal, action, "connection", str(row["id"]), trace_id=trace, payload=payload)
    conn.execute(insert(outbox_events).values(tenant_id=principal.tenant_id, type=action, payload=payload))


# ---------------------------------------------------------------- reauthorize follow-up (PUT /credential)


def resume_after_reauthorization(
    principal: Principal, connection_id: uuid.UUID, transports: dict[str, Any], trace: Any
) -> tuple[list[dict], list[dict]]:
    """After a new credential is stored: check health (which resumes access-paused sources that now pass),
    then queue a reconcile for every source that resumed. Returns (resumed, still_blocked)."""
    from app.modules.connections import run_health_check  # noqa: PLC0415 (import cycle)

    with tenant_session(principal.tenant_id) as conn:
        row = dict(
            conn.execute(select(source_connections).where(source_connections.c.id == connection_id))
            .mappings()
            .one()
        )
        before = {s["source_id"]: s for s in connection_sources(conn, principal.tenant_id, row)}
    updated = run_health_check(principal, connection_id, transports, trace, always_record=True)
    with tenant_session(principal.tenant_id) as conn:
        after = connection_sources(conn, principal.tenant_id, updated)
        resumed = [
            s
            for s in after
            if s["status"] == "ACTIVE"
            and before.get(s["source_id"], {}).get("status") == "PAUSED"
            and before[s["source_id"]]["pause_cause"] == "access"
        ]
        blocked = [s for s in after if s["status"] == "PAUSED" and s["pause_cause"] == "access"]
        for s in resumed:
            queue.enqueue(
                conn,
                principal.tenant_id,
                s["project_id"],
                s["kind"],
                s["source_id"],
                "manual",
                hints={"after": "reauthorization"},
            )
    return resumed, blocked


# ---------------------------------------------------------------- routes


@router.get(
    "/v1/connections/attention",
    response_model=list[AttentionItem],
    summary="Connections and sources that need an admin: invalid, degraded, failing, stale or paused",
    responses={403: PROBLEM},
)
def attention(request: Request, principal: CurrentPrincipal) -> list[AttentionItem]:
    require_tenant(principal, "tenant.connection.manage", request, target_type="connection")
    items = []
    with tenant_session(principal.tenant_id) as conn:
        rows = [
            dict(r)
            for r in conn.execute(
                select(source_connections).order_by(source_connections.c.display_name)
            ).mappings()
        ]
        for row in rows:
            sources = connection_sources(conn, principal.tenant_id, row)
            problems = []
            if row["status"] == "REVOKED":
                if any(s["pause_cause"] == "connection_revoked" for s in sources):
                    problems.append(explain("REVOKED"))
            else:
                if row["status"] in ("INVALID", "DEGRADED"):
                    for issue in (row["health"] or {}).get("issues", []):
                        if issue.get("severity") != "warning":
                            problems.append(
                                (explain(issue["code"]) or {})
                                | {k: issue[k] for k in ("repository", "project", "space") if k in issue}
                            )
                problems += [
                    (s["problem"] or {}) | {"source": s["name"], "project": s["project_key"]}
                    for s in sources
                    if s["problem"] and s["freshness"] != "paused"
                ]
            if problems:
                affected = [s for s in sources if s["freshness"] in ("failing", "paused", "stale")]
                items.append(
                    AttentionItem(
                        connection_id=row["id"],
                        display_name=row["display_name"],
                        provider=row["provider"],
                        status=row["status"],
                        problems=problems,
                        affected_sources=len(affected),
                    )
                )
    return items


@router.get(
    "/v1/connections/{connection_id}/sources",
    response_model=list[ConnectionSource],
    summary="Every source using this connection, across projects, with freshness and problems",
    responses={403: PROBLEM, 404: PROBLEM},
)
def list_connection_sources(
    request: Request, principal: CurrentPrincipal, connection_id: uuid.UUID
) -> list[ConnectionSource]:
    require_tenant(
        principal, "tenant.connection.manage", request, target_type="connection", target_id=str(connection_id)
    )
    with tenant_session(principal.tenant_id) as conn:
        row = _connection(conn, principal, request, connection_id)
        return [
            ConnectionSource.model_validate(s) for s in connection_sources(conn, principal.tenant_id, row)
        ]


@router.get(
    "/v1/connections/{connection_id}/impact",
    response_model=Impact,
    summary="What disconnecting (revoking) this connection would stop: projects, sources and webhooks",
    responses={403: PROBLEM, 404: PROBLEM},
)
def impact(request: Request, principal: CurrentPrincipal, connection_id: uuid.UUID) -> Impact:
    require_tenant(
        principal, "tenant.connection.manage", request, target_type="connection", target_id=str(connection_id)
    )
    with tenant_session(principal.tenant_id) as conn:
        row = _connection(conn, principal, request, connection_id)
        sources = [
            s
            for s in connection_sources(conn, principal.tenant_id, row)
            if s["pause_cause"] != "connection_revoked"
        ]
        hooks = 0
        if row["provider"] == "github":
            hooks = conn.execute(
                select(project_repositories.c.id).where(
                    project_repositories.c.connection_id == connection_id,
                    project_repositories.c.webhook_status == "registered",
                )
            ).all()
            hooks = len(hooks)
        secret = conn.execute(
            select(connection_secrets.c.purpose).where(
                connection_secrets.c.connection_id == connection_id, connection_secrets.c.purpose == "webhook"
            )
        ).first()
    project_ids = {s["project_id"] for s in sources}
    active = [s for s in sources if s["status"] == "ACTIVE"]
    return Impact(
        connection_id=row["id"],
        display_name=row["display_name"],
        status=row["status"],
        projects=len(project_ids),
        sources=[ConnectionSource.model_validate(s) for s in sources],
        webhooks={"registered_repository_hooks": hooks, "webhook_secret": 1 if secret else 0},
        summary=(
            f"Disconnecting stops syncing {len(active)} active source(s) in {len(project_ids)} project(s), "
            "destroys the stored token and rejects its webhooks. History is kept; a new connection can adopt "
            "these sources later."
        ),
    )


def _verify(kind: str, provider: Any, source: dict[str, Any]) -> str | None:
    """None when the new credential can read the source; otherwise the reason."""
    try:
        if kind == "git":
            repo = provider.get_repository(source["provider_repo_id"])
            if not repo.can_read:
                return "the new credential cannot read this repository"
            missing = [
                b for b in source["branches"] if not provider.branch_exists(source["provider_repo_id"], b)
            ]
            return f"branch not found: {', '.join(missing)}" if missing else None
        if kind == "jira":
            if not provider.my_permissions(source["jira_key"]).get(READ_PERMISSION):
                return "the new credential cannot browse this Jira project"
            return None
        space = provider.get_space(source["space_key"])
        return None if space.id == source["space_id"] else "the space key now points at a different space"
    except NotFoundOrNoAccess:
        return "the new credential cannot read this source"


@router.post(
    "/v1/connections/{connection_id}/adopt",
    response_model=AdoptResult,
    summary="Move a revoked connection's sources to this connection (same provider and site), verifying each",
    responses={403: PROBLEM, 404: PROBLEM, 409: PROBLEM, 502: PROBLEM, 503: PROBLEM},
)
def adopt(
    request: Request,
    principal: CurrentPrincipal,
    transports: Transports,
    connection_id: uuid.UUID,
    payload: Annotated[AdoptRequest, Body()],
) -> AdoptResult:
    require_tenant(
        principal, "tenant.connection.manage", request, target_type="connection", target_id=str(connection_id)
    )
    with tenant_session(principal.tenant_id) as conn:
        target = _connection(conn, principal, request, connection_id)
        old = _connection(conn, principal, request, payload.from_connection_id)
        if target["status"] not in ("ACTIVE", "DEGRADED"):
            raise conflict(
                "CONNECTION_NOT_ACTIVE", f"This connection is {target['status']}; reauthorize it first"
            )
        if old["status"] != "REVOKED":
            raise conflict(
                "ADOPT_NOT_ALLOWED", "Only a revoked connection's sources can be adopted; revoke it first"
            )
        if (old["provider"], old["base_url"]) != (target["provider"], target["base_url"]):
            raise conflict(
                "ADOPT_NOT_ALLOWED",
                "Sources can only move between connections to the same site "
                f"({old['provider']} {old['base_url']})",
            )
        kind = PROVIDER_KIND[target["provider"]]
        table, name = TABLES[kind]
        candidates = [
            dict(r)
            for r in conn.execute(
                select(table).where(
                    table.c.tenant_id == principal.tenant_id, table.c.connection_id == old["id"]
                )
            ).mappings()
        ]
        taken = {
            (r.project_id, r.key)
            for r in conn.execute(
                select(
                    table.c.project_id,
                    (
                        table.c.provider_repo_id
                        if kind == "git"
                        else table.c.jira_project_id
                        if kind == "jira"
                        else table.c.space_id
                    ).label("key"),
                ).where(table.c.tenant_id == principal.tenant_id, table.c.connection_id == target["id"])
            )
        }
        token = credential_of(conn, principal, target)
        archived = _archived(conn, {c["project_id"] for c in candidates})
    provider = (
        provider_for(target["provider"], target["base_url"], token, transports)
        if kind == "git"
        else jira_for(target, token, transports)
        if kind == "jira"
        else confluence_for(target, token, transports)
    )
    natural = {"git": "provider_repo_id", "jira": "jira_project_id", "confluence": "space_id"}[kind]
    verdicts: dict[uuid.UUID, str | None] = {}
    skipped = []
    archived_reason = "project is archived (read-only); restore it, then adopt again"

    def skip(source: dict[str, Any], reason: str) -> None:
        skipped.append({"source_id": str(source["id"]), "name": source[name], "reason": reason})

    with provider_errors():
        for source in candidates:
            if source["project_id"] in archived:  # INPROOM-150: archived projects are read-only
                skip(source, archived_reason)
                continue
            if (source["project_id"], source[natural]) in taken:
                skipped.append(
                    {
                        "source_id": str(source["id"]),
                        "name": source[name],
                        "reason": "already selected through this connection",
                    }
                )
                continue
            verdicts[source["id"]] = (
                None if source["pause_cause"] == "excluded" else _verify(kind, provider, source)
            )
    now = datetime.now(UTC)
    with tenant_session(principal.tenant_id) as conn:
        _connection(conn, principal, request, connection_id, lock=True)
        # Re-check under the project locks (archiving takes the same lock), so a project archived while the
        # provider was being asked is never modified (INPROOM-150).
        archived_now = _archived(
            conn, {c["project_id"] for c in candidates if c["id"] in verdicts}, lock=True
        )
        for source in candidates:
            if source["id"] not in verdicts:
                continue
            if source["project_id"] in archived_now:
                del verdicts[source["id"]]
                skip(source, archived_reason)
                continue
            reason = verdicts[source["id"]]
            values: dict[str, Any] = {
                "connection_id": target["id"],
                "updated_at": now,
                "version": table.c.version + 1,
            }
            if source["pause_cause"] == "excluded":
                pass  # moves with its exclusion intact: only "include" resumes it
            elif reason is None:
                values |= {"status": "ACTIVE", "status_reason": None, "pause_cause": None}
            else:
                values |= {"status": "PAUSED", "status_reason": reason, "pause_cause": "access"}
            if kind == "git":
                values |= {"webhook_id": None, "webhook_status": "none"}  # hooks belonged to the old secret
            conn.execute(
                update(table)
                .where(table.c.tenant_id == principal.tenant_id, table.c.id == source["id"])
                .values(**values)
            )
            if source["pause_cause"] != "excluded" and reason is None:
                queue.enqueue(
                    conn,
                    principal.tenant_id,
                    source["project_id"],
                    kind,
                    source["id"],
                    "manual",
                    hints={"after": "adoption"},
                )
        after = {s["source_id"]: s for s in connection_sources(conn, principal.tenant_id, target)}
        moved = [after[i] for i in verdicts if i in after]
        result = AdoptResult(
            adopted=[ConnectionSource.model_validate(s) for s in moved if s["status"] == "ACTIVE"],
            still_blocked=[ConnectionSource.model_validate(s) for s in moved if s["pause_cause"] == "access"],
            kept_excluded=[
                ConnectionSource.model_validate(s) for s in moved if s["pause_cause"] == "excluded"
            ],
            skipped=skipped,
        )
        if moved or skipped:
            _emit(
                conn,
                principal,
                request.state.trace_id,
                "connection.sources_adopted",
                target,
                {
                    "from_connection_id": str(old["id"]),
                    "adopted": len(result.adopted),
                    "still_blocked": len(result.still_blocked),
                    "kept_excluded": len(result.kept_excluded),
                    "skipped": len(skipped),
                },
            )
    return result


def _archived(conn: Connection, project_ids: set[uuid.UUID], lock: bool = False) -> set[uuid.UUID]:
    if not project_ids:
        return set()
    stmt = select(projects.c.id, projects.c.status).where(projects.c.id.in_(project_ids))
    if lock:
        stmt = stmt.with_for_update()
    return {r.id for r in conn.execute(stmt) if r.status == "ARCHIVED"}


def skip_queued_jobs(conn: Connection, tenant_id: uuid.UUID, row: dict[str, Any]) -> int:
    """On revoke: jobs still queued for the connection or its sources will never run."""
    kind = PROVIDER_KIND[row["provider"]]
    table, _ = TABLES[kind]
    ids = [
        r.id
        for r in conn.execute(
            select(table.c.id).where(table.c.tenant_id == tenant_id, table.c.connection_id == row["id"])
        )
    ]
    stmt = (
        update(sync_jobs)
        .where(
            sync_jobs.c.tenant_id == tenant_id,
            sync_jobs.c.status == "QUEUED",
            ((sync_jobs.c.source_kind == kind) & sync_jobs.c.source_id.in_(ids))
            | ((sync_jobs.c.source_kind == "connection") & (sync_jobs.c.source_id == row["id"])),
        )
        .values(status="SKIPPED", finished_at=datetime.now(UTC), result={"skipped": "connection revoked"})
    )
    return conn.execute(stmt).rowcount
