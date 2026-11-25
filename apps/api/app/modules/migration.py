"""Migration projects: plan and inventory scan APIs (INPROOM-151 / story INPROOM-19).

A MIGRATION project names the Jira project that stays authoritative; a scan inventories it (worker job, see
app.migration.scan). Review, baseline confirmation and drift build on this in INPROOM-152.
"""

import uuid
from datetime import UTC, date, datetime
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Body, Header, Request, Response
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field
from sqlalchemy import Connection, insert, select, update

from app import audit
from app.api_errors import ApiError, conflict, precondition_failed
from app.auth import CurrentPrincipal, Principal
from app.authz import authorize
from app.db import tenant_session
from app.db_schema import migration_plans, migration_runs, project_jira_sources
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
                    migration_runs.c.plan_id == plan["id"], migration_runs.c.status.in_(("QUEUED", "RUNNING"))
                )
            )
            .mappings()
            .first()
        )
        if run is None:
            run = (
                conn.execute(
                    insert(migration_runs)
                    .values(tenant_id=principal.tenant_id, project_id=project_id, plan_id=plan["id"])
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
