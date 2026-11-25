"""Migration projects: plan and inventory scan APIs (INPROOM-151 / story INPROOM-19).

A MIGRATION project names the Jira project that stays authoritative; a scan inventories it (worker job, see
app.migration.scan). Review, baseline confirmation and drift build on this in INPROOM-152.
"""

import base64
import uuid
from datetime import UTC, date, datetime, timedelta
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Body, Header, Query, Request, Response
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field
from sqlalchemy import BigInteger, Connection, cast, func, insert, select, update

from app import audit
from app.api_errors import ApiError, conflict, precondition_failed
from app.auth import CurrentPrincipal, Principal
from app.authz import authorize
from app.db import tenant_session
from app.db_schema import (
    migration_issues,
    migration_plans,
    migration_runs,
    outbox_events,
    project_jira_sources,
    projects,
)
from app.modules import settings as settings_module
from app.sync import queue

router = APIRouter(tags=["migration"])
PROBLEM = {"description": "problem+json"}


class PlanRequest(BaseModel):
    jira_source_id: uuid.UUID = Field(
        description="One of this project's selected Jira projects (stays authoritative)"
    )
    import_mode: Literal["jira_authoritative"] = "jira_authoritative"
    history_since: date | None = Field(
        default=None, description="Only issues updated since this date (omit for the full history)"
    )


class MigrationRun(BaseModel):
    id: uuid.UUID
    status: str
    pages_done: int
    issues_seen: int
    counts: dict[str, Any]
    diagnostics: dict[str, Any]
    scanned_as: dict[str, Any]
    error: dict[str, Any] | None
    created_at: datetime
    started_at: datetime | None
    finished_at: datetime | None


class MigrationPlan(BaseModel):
    id: uuid.UUID
    project_id: uuid.UUID
    jira_source_id: uuid.UUID
    jira_key: str | None = None
    import_mode: str
    history_since: date | None
    status: str
    baseline: dict[str, Any] | None
    created_by: str
    version: int


class MigrationState(BaseModel):
    project_id: uuid.UUID
    project_mode: str
    project_status: str
    plan: MigrationPlan | None
    latest_run: MigrationRun | None
    write_readiness: dict[str, Any] = Field(default_factory=dict)


def plan_of(conn: Connection, project_id: uuid.UUID) -> dict[str, Any] | None:
    row = (
        conn.execute(
            select(migration_plans, project_jira_sources.c.jira_key)
            .join(project_jira_sources, project_jira_sources.c.id == migration_plans.c.jira_source_id)
            .where(migration_plans.c.project_id == project_id)
        )
        .mappings()
        .first()
    )
    return dict(row) if row else None


def latest_run(conn: Connection, project_id: uuid.UUID) -> dict[str, Any] | None:
    row = (
        conn.execute(
            select(migration_runs)
            .where(migration_runs.c.project_id == project_id)
            .order_by(migration_runs.c.created_at.desc())
            .limit(1)
        )
        .mappings()
        .first()
    )
    return dict(row) if row else None


def supersede_runs(conn: Connection, plan: dict[str, Any]) -> int:
    """Queued / running scans of an OLDER plan revision can never qualify for a baseline (INPROOM-155)."""
    return conn.execute(
        update(migration_runs)
        .where(
            migration_runs.c.plan_id == plan["id"],
            migration_runs.c.status.in_(("QUEUED", "RUNNING")),
            migration_runs.c.plan_version != plan["version"],
        )
        .values(
            status="FAILED",
            finished_at=func.now(),
            error={"code": "PLAN_CHANGED", "message": "the plan changed; this scan was superseded"},
        )
    ).rowcount


def require_migration_project(project: dict[str, Any]) -> None:
    if project["mode"] != "MIGRATION":
        raise conflict("NOT_A_MIGRATION_PROJECT", "Only projects created with mode MIGRATION can be migrated")
    if project["status"] == "ARCHIVED":
        raise conflict("PROJECT_ARCHIVED", "Archived projects are read-only; restore the project first")


def _audit(conn, principal: Principal, request: Request, action: str, project_id, payload: dict) -> None:
    audit.record(
        conn,
        principal,
        action,
        "migration",
        str(project_id),
        trace_id=request.state.trace_id,
        payload=payload,
        project_id=project_id,
    )


def state(conn: Connection, project: dict[str, Any]) -> MigrationState:
    plan = plan_of(conn, project["id"])
    run = latest_run(conn, project["id"])
    return MigrationState(
        project_id=project["id"],
        project_mode=project["mode"],
        project_status=project["status"],
        plan=MigrationPlan.model_validate(plan) if plan else None,
        latest_run=MigrationRun.model_validate(run) if run else None,
        write_readiness=readiness(conn, plan, run),
    )


@router.get(
    "/v1/projects/{project_id}/migration",
    response_model=MigrationState,
    summary="The project's migration plan and latest inventory scan",
    responses={404: PROBLEM},
)
def get_migration(request: Request, principal: CurrentPrincipal, project_id: uuid.UUID) -> MigrationState:
    with tenant_session(principal.tenant_id) as conn:
        access = authorize(conn, principal, request, "project.read", project_id)
        return state(conn, access.project)


@router.put(
    "/v1/projects/{project_id}/migration",
    response_model=MigrationState,
    summary="Create or update the migration plan (If-Match once it exists); MIGRATION projects only",
    responses={404: PROBLEM, 409: PROBLEM, 412: PROBLEM, 422: PROBLEM, 428: PROBLEM},
)
def put_plan(
    request: Request,
    response: Response,
    principal: CurrentPrincipal,
    project_id: uuid.UUID,
    payload: Annotated[PlanRequest, Body()],
    if_match: Annotated[str | None, Header(alias="If-Match")] = None,
) -> MigrationState:
    with tenant_session(principal.tenant_id) as conn:
        access = authorize(conn, principal, request, "project.source.manage", project_id, lock=True)
        require_migration_project(access.project)
        source = (
            conn.execute(
                select(project_jira_sources).where(
                    project_jira_sources.c.id == payload.jira_source_id,
                    project_jira_sources.c.project_id == project_id,
                )
            )
            .mappings()
            .first()
        )
        if source is None:
            raise ApiError(
                422,
                "JIRA_SOURCE_NOT_SELECTED",
                "Unprocessable request",
                "Select the Jira project for this project on Sources first, then choose it here",
            )
        existing = plan_of(conn, project_id)
        values = {
            "jira_source_id": payload.jira_source_id,
            "import_mode": payload.import_mode,
            "history_since": payload.history_since,
        }
        if existing is None:
            conn.execute(
                insert(migration_plans).values(
                    tenant_id=principal.tenant_id,
                    project_id=project_id,
                    created_by=principal.user_id,
                    **values,
                )
            )
            _audit(
                conn,
                principal,
                request,
                "migration.plan.created",
                project_id,
                {"jira_key": source["jira_key"]},
            )
        else:
            expected = settings_module.parse_if_match(if_match, "migration plan")
            if existing["version"] != expected:
                raise precondition_failed(
                    "The plan was changed by someone else; current ETag is "
                    f"{settings_module.etag(existing['version'])}"
                )
            if existing["status"] == "BASELINED":
                raise conflict("PLAN_BASELINED", "The baseline is confirmed; the plan can no longer change")
            if any(existing[k] != v for k, v in values.items()):
                conn.execute(
                    update(migration_plans)
                    .where(migration_plans.c.id == existing["id"])
                    .values(
                        **values,
                        status="DRAFT",
                        updated_at=datetime.now(UTC),
                        version=migration_plans.c.version + 1,
                    )
                )
                supersede_runs(conn, plan_of(conn, project_id))
                _audit(
                    conn,
                    principal,
                    request,
                    "migration.plan.updated",
                    project_id,
                    {"jira_key": source["jira_key"]},
                )
        current = state(conn, access.project)
    response.headers["ETag"] = settings_module.etag(current.plan.version)
    return current


@router.post(
    "/v1/projects/{project_id}/migration/scan",
    status_code=202,
    response_model=MigrationRun,
    summary="Inventory the authoritative Jira project now (worker job; resumable; coalesced)",
    responses={404: PROBLEM, 409: PROBLEM},
)
def scan(request: Request, principal: CurrentPrincipal, project_id: uuid.UUID) -> JSONResponse:
    with tenant_session(principal.tenant_id) as conn:
        access = authorize(conn, principal, request, "project.source.manage", project_id, lock=True)
        require_migration_project(access.project)
        plan = plan_of(conn, project_id)
        if plan is None:
            raise conflict("PLAN_REQUIRED", "Save a migration plan (choose the Jira project) before scanning")
        source = conn.execute(
            select(project_jira_sources.c.status).where(project_jira_sources.c.id == plan["jira_source_id"])
        ).scalar_one()
        if source != "ACTIVE":
            raise conflict("SOURCE_PAUSED", "The plan's Jira source is paused; check its connection first")
        run = (
            conn.execute(
                select(migration_runs).where(
                    migration_runs.c.plan_id == plan["id"],
                    migration_runs.c.status.in_(("QUEUED", "RUNNING")),
                    migration_runs.c.plan_version == plan["version"],
                )
            )
            .mappings()
            .first()
        )
        if run is None:
            supersede_runs(conn, plan)
            run = (
                conn.execute(
                    insert(migration_runs)
                    .values(
                        tenant_id=principal.tenant_id,
                        project_id=project_id,
                        plan_id=plan["id"],
                        plan_version=plan["version"],
                    )
                    .returning(*migration_runs.c)
                )
                .mappings()
                .one()
            )
            _audit(
                conn, principal, request, "migration.scan.requested", project_id, {"run_id": str(run["id"])}
            )
        queue.enqueue(conn, principal.tenant_id, project_id, "migration", plan["id"], "manual")
    return JSONResponse(MigrationRun.model_validate(dict(run)).model_dump(mode="json"), status_code=202)


# ---------------------------------------------------------------- review, baseline, drift (INPROOM-152)

COMPARED = ("key", "type", "status", "parent_issue_id", "summary")
SCAN_MAX_AGE = timedelta(hours=24)


class InventoryIssue(BaseModel):
    jira_issue_id: str
    key: str
    original_key: str
    type: str
    type_raw: str
    status: str
    status_raw: str
    parent_issue_id: str | None
    parent_key: str | None
    summary: str
    creator: str | None
    reporter: str | None
    created_at: str | None
    updated_at: str | None
    links: list[dict[str, Any]]
    flags: list[str]
    present: bool
    children: int = 0


class InventoryPage(BaseModel):
    items: list[InventoryIssue]
    next_cursor: str | None


class Drift(BaseModel):
    baseline_at: datetime | None
    added: list[dict[str, Any]]
    changed: list[dict[str, Any]]
    removed: list[dict[str, Any]]
    counts: dict[str, int]


def readiness(conn: Connection, plan: dict[str, Any] | None, run: dict[str, Any] | None) -> dict[str, Any]:
    """Would automated writes (E07 publishing) be safe right now? Reasons are stable codes."""
    reasons = []
    if plan is None or plan["status"] != "BASELINED":
        reasons.append("NO_BASELINE")
    if run is None:
        reasons.append("SCAN_REQUIRED")
    elif run["status"] == "FAILED":
        reasons.append("SCAN_FAILED")
    elif run["status"] in ("QUEUED", "RUNNING"):
        reasons.append("SCAN_IN_PROGRESS")
    elif run["finished_at"] and datetime.now(UTC) - run["finished_at"] > SCAN_MAX_AGE:
        reasons.append("SCAN_STALE")
    if plan is not None:
        source = (
            conn.execute(
                select(project_jira_sources.c.status, project_jira_sources.c.capabilities).where(
                    project_jira_sources.c.id == plan["jira_source_id"]
                )
            )
            .mappings()
            .first()
        )
        if source is None or source["status"] != "ACTIVE":
            reasons.append("SOURCE_PAUSED")
        elif not (source["capabilities"] or {}).get("read", True):
            reasons.append("PERMISSION_LOST")
    return {"ready": not reasons, "reasons": reasons}


def _encode(value: str) -> str:
    return base64.urlsafe_b64encode(value.encode()).decode().rstrip("=")


def _decode(cursor: str) -> str:
    try:
        return base64.urlsafe_b64decode(cursor + "=" * (-len(cursor) % 4)).decode()
    except ValueError as exc:
        raise ApiError(400, "INVALID_CURSOR", "Invalid cursor", "cursor is malformed") from exc


@router.get(
    "/v1/projects/{project_id}/migration/issues",
    response_model=InventoryPage,
    summary="The inventory (Jira keys, hierarchy, flags); filter by type, flag or parent ('root' = top)",
    responses={400: PROBLEM, 404: PROBLEM},
)
def list_issues(
    request: Request,
    principal: CurrentPrincipal,
    project_id: uuid.UUID,
    type: Annotated[str | None, Query(pattern="^(epic|story|task|subtask|bug|other)$")] = None,  # noqa: A002
    flag: Annotated[
        str | None, Query(pattern="^(orphan|unsupported_type|unresolved_link|key_changed|duplicate_key)$")
    ] = None,
    parent: Annotated[str | None, Query(max_length=40)] = None,
    present: bool = True,
    limit: Annotated[int, Query(ge=1, le=200)] = 100,
    cursor: Annotated[str | None, Query(max_length=200)] = None,
) -> InventoryPage:
    t = migration_issues
    order = cast(t.c.jira_issue_id, BigInteger)
    stmt = select(t).where(t.c.project_id == project_id, t.c.present.is_(present))
    if type:
        stmt = stmt.where(t.c.type == type)
    if flag:
        stmt = stmt.where(t.c.flags.contains([flag]))
    if parent == "root":
        stmt = stmt.where(t.c.parent_issue_id.is_(None))
    elif parent:
        stmt = stmt.where(t.c.parent_issue_id == parent)
    if cursor:
        stmt = stmt.where(order > int(_decode(cursor)))
    stmt = stmt.order_by(order).limit(limit + 1)
    with tenant_session(principal.tenant_id) as conn:
        authorize(conn, principal, request, "project.read", project_id)
        rows = [dict(r) for r in conn.execute(stmt).mappings()]
        more = len(rows) > limit
        rows = rows[:limit]
        ids = [r["jira_issue_id"] for r in rows]
        counts = (
            dict(
                conn.execute(
                    select(t.c.parent_issue_id, func.count())
                    .where(t.c.project_id == project_id, t.c.parent_issue_id.in_(ids), t.c.present.is_(True))
                    .group_by(t.c.parent_issue_id)
                ).all()
            )
            if ids
            else {}
        )
    return InventoryPage(
        items=[
            InventoryIssue.model_validate(r | {"children": counts.get(r["jira_issue_id"], 0)}) for r in rows
        ],
        next_cursor=_encode(rows[-1]["jira_issue_id"]) if more else None,
    )


@router.post(
    "/v1/projects/{project_id}/migration/confirm",
    response_model=MigrationState,
    summary="Confirm the reviewed inventory as the project's baseline (If-Match); activates the project",
    responses={404: PROBLEM, 409: PROBLEM, 412: PROBLEM, 428: PROBLEM},
)
def confirm(
    request: Request,
    response: Response,
    principal: CurrentPrincipal,
    project_id: uuid.UUID,
    if_match: Annotated[str | None, Header(alias="If-Match")] = None,
) -> MigrationState:
    with tenant_session(principal.tenant_id) as conn:
        # authorize first: an unauthorized caller gets the masked / audited denial, never a 428 hint
        access = authorize(conn, principal, request, "project.source.manage", project_id, lock=True)
        expected = settings_module.parse_if_match(if_match, "migration plan")
        require_migration_project(access.project)
        plan = plan_of(conn, project_id)
        if plan is None:
            raise conflict("PLAN_REQUIRED", "Save a migration plan and scan before confirming")
        if plan["version"] != expected:
            raise precondition_failed(
                "The plan was changed by someone else; current ETag is "
                f"{settings_module.etag(plan['version'])}"
            )
        if plan["status"] == "BASELINED":
            raise conflict("PLAN_BASELINED", "The baseline is already confirmed")
        run = latest_run(conn, project_id)
        if run is None or run["status"] != "SUCCEEDED" or run["plan_version"] != plan["version"]:
            # INPROOM-155: only a completed scan of THIS plan revision (source + history scope) qualifies
            raise conflict(
                "SCAN_REQUIRED",
                "Confirm needs a completed scan of the current plan (source and scope); scan it, then review",
            )
        if datetime.now(UTC) - run["finished_at"] > SCAN_MAX_AGE:
            raise conflict("SCAN_STALE", "The last scan is older than 24 hours; scan again before confirming")
        now = datetime.now(UTC)
        conn.execute(
            update(migration_issues)
            .where(migration_issues.c.project_id == project_id, migration_issues.c.present.is_(True))
            .values(
                baseline_snapshot=func.jsonb_build_object(
                    *[x for c in COMPARED for x in (c, migration_issues.c[c])]
                )
            )
        )
        baseline = {
            "run_id": str(run["id"]),
            "counts": run["counts"],
            "scope": {
                "jira_key": plan["jira_key"],
                "import_mode": plan["import_mode"],
                "history_since": plan["history_since"].isoformat() if plan["history_since"] else None,
            },
            "scanned_at": run["finished_at"].isoformat(),
            "confirmed_by": principal.user_id,
            "confirmed_at": now.isoformat(),
            "warnings": ["EMPTY_INVENTORY"] if not run["counts"].get("issues") else [],
        }
        conn.execute(
            update(migration_plans)
            .where(migration_plans.c.id == plan["id"])
            .values(
                status="BASELINED", baseline=baseline, updated_at=now, version=migration_plans.c.version + 1
            )
        )
        _audit(conn, principal, request, "migration.baselined", project_id, baseline)
        conn.execute(
            insert(outbox_events).values(
                tenant_id=principal.tenant_id,
                type="migration.baselined",
                payload={"project_id": str(project_id)} | baseline,
            )
        )
        if access.project["status"] == "PENDING_SOURCES":
            conn.execute(
                update(projects)
                .where(projects.c.tenant_id == principal.tenant_id, projects.c.id == project_id)
                .values(status="ACTIVE", version=projects.c.version + 1, updated_at=now)
            )
            change = {
                "project_id": str(project_id),
                "from": "PENDING_SOURCES",
                "to": "ACTIVE",
                "reason": "migration baseline confirmed",
            }
            _audit(conn, principal, request, "project.activate", project_id, change)
            conn.execute(
                insert(outbox_events).values(
                    tenant_id=principal.tenant_id,
                    type="project.status.changed",
                    payload=change | {"action": "activate", "changed_by": principal.user_id},
                )
            )
        project = conn.execute(select(projects).where(projects.c.id == project_id)).mappings().one()
        current = state(conn, dict(project))
    response.headers["ETag"] = settings_module.etag(current.plan.version)
    return current


@router.get(
    "/v1/projects/{project_id}/migration/drift",
    response_model=Drift,
    summary="What changed in Jira since the confirmed baseline (the baseline itself never moves)",
    responses={404: PROBLEM, 409: PROBLEM},
)
def drift(request: Request, principal: CurrentPrincipal, project_id: uuid.UUID) -> Drift:
    t = migration_issues
    with tenant_session(principal.tenant_id) as conn:
        access = authorize(conn, principal, request, "project.read", project_id)
        plan = plan_of(conn, project_id)
        if plan is None or plan["status"] != "BASELINED":
            raise conflict("NO_BASELINE", "Confirm a baseline first; drift is measured against it")
        rows = [
            dict(r)
            for r in conn.execute(
                select(t).where(
                    t.c.project_id == project_id,
                    (t.c.present.is_(True)) | (t.c.baseline_snapshot.is_not(None)),
                )
            ).mappings()
        ]
    del access
    added, changed, removed = [], [], []
    for r in rows:
        snap = r["baseline_snapshot"]
        if snap is None:
            added.append(
                {"id": r["jira_issue_id"], "key": r["key"], "type": r["type"], "summary": r["summary"]}
            )
        elif not r["present"]:
            removed.append({"id": r["jira_issue_id"], "key": snap["key"], "summary": snap["summary"]})
        else:
            diffs = {c: {"from": snap[c], "to": r[c]} for c in COMPARED if snap.get(c) != r[c]}
            if diffs:
                changed.append({"id": r["jira_issue_id"], "key": r["key"], "changes": diffs})
    limit = 200
    return Drift(
        baseline_at=datetime.fromisoformat(plan["baseline"]["confirmed_at"]),
        added=added[:limit],
        changed=changed[:limit],
        removed=removed[:limit],
        counts={"added": len(added), "changed": len(changed), "removed": len(removed)},
    )
