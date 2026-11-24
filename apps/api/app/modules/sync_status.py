"""Source sync status, manual reconcile and job history (INPROOM-144 / story INPROOM-25).

Freshness per selected source comes from source_sync_state and the job queue:
- fresh    last success within 3 x the sync interval;
- stale    no success yet, or the last one is older than that;
- failing  the latest attempts failed (consecutive_failures > 0);
plus whether a job is queued or running. "Reconcile now" enqueues a coalesced manual job.
"""

import uuid
from datetime import UTC, datetime, timedelta
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Body, Path, Query, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field
from sqlalchemy import Connection, select

from app.api_errors import ApiError, conflict
from app.auth import CurrentPrincipal
from app.authz import authorize
from app.config import get_settings
from app.db import tenant_session
from app.db_schema import (
    project_confluence_sources,
    project_jira_sources,
    project_repositories,
    source_sync_state,
    sync_jobs,
)
from app.sync import queue

router = APIRouter(tags=["sync"])
PROBLEM = {"description": "problem+json"}
Kind = Literal["git", "jira", "confluence"]
TABLES = {
    "git": (project_repositories, "full_name"),
    "jira": (project_jira_sources, "jira_key"),
    "confluence": (project_confluence_sources, "space_key"),
}


class SourceSyncStatus(BaseModel):
    kind: str
    source_id: uuid.UUID
    name: str
    source_status: str
    health: Literal["fresh", "stale", "failing"]
    last_success_at: datetime | None
    last_attempt_at: datetime | None
    next_due_at: datetime | None
    consecutive_failures: int
    last_error: dict[str, Any] | None
    queued: bool
    running: bool


class SyncStatus(BaseModel):
    project_id: uuid.UUID
    interval_seconds: int
    sources: list[SourceSyncStatus]


class SyncJob(BaseModel):
    id: uuid.UUID
    source_kind: str
    source_id: uuid.UUID
    reason: str
    mode: str
    status: str
    attempts: int
    triggers: int
    run_after: datetime
    created_at: datetime
    started_at: datetime | None
    finished_at: datetime | None
    last_error: dict[str, Any] | None
    result: dict[str, Any]


class ReconcileRequest(BaseModel):
    full: bool = Field(default=False, description="Also re-check deletions/schema (Jira) and access")


def _sources(conn: Connection, tenant_id: uuid.UUID, project_id: uuid.UUID) -> list[dict[str, Any]]:
    out = []
    for kind, (table, name) in TABLES.items():
        rows = conn.execute(
            select(table.c.id, table.c.status, table.c[name].label("name")).where(
                table.c.tenant_id == tenant_id, table.c.project_id == project_id
            )
        ).all()
        out += [{"kind": kind, "source_id": r.id, "name": r.name, "source_status": r.status} for r in rows]
    return sorted(out, key=lambda s: (s["kind"], s["name"]))


@router.get(
    "/v1/projects/{project_id}/sources/sync-status",
    response_model=SyncStatus,
    summary="Freshness of every selected source: last success, next due, failures, queued/running",
    responses={404: PROBLEM},
)
def sync_status(request: Request, principal: CurrentPrincipal, project_id: uuid.UUID) -> SyncStatus:
    interval = get_settings().sync_interval_seconds
    with tenant_session(principal.tenant_id) as conn:
        authorize(conn, principal, request, "project.read", project_id)
        selected = _sources(conn, principal.tenant_id, project_id)
        states = {
            (r["source_kind"], r["source_id"]): dict(r)
            for r in conn.execute(
                select(source_sync_state).where(source_sync_state.c.project_id == project_id)
            ).mappings()
        }
        active = conn.execute(
            select(sync_jobs.c.source_kind, sync_jobs.c.source_id, sync_jobs.c.status).where(
                sync_jobs.c.project_id == project_id, sync_jobs.c.status.in_(("QUEUED", "RUNNING"))
            )
        ).all()
    stale_before = datetime.now(UTC) - timedelta(seconds=3 * interval)
    items = []
    for s in selected:
        state = states.get((s["kind"], s["source_id"]), {})
        failures = state.get("consecutive_failures", 0)
        last = state.get("last_success_at")
        health = "failing" if failures else ("fresh" if last and last >= stale_before else "stale")
        jobs = {j.status for j in active if (j.source_kind, j.source_id) == (s["kind"], s["source_id"])}
        items.append(
            SourceSyncStatus(
                **s,
                health=health,
                last_success_at=last,
                last_attempt_at=state.get("last_attempt_at"),
                next_due_at=state.get("next_due_at"),
                consecutive_failures=failures,
                last_error=state.get("last_error"),
                queued="QUEUED" in jobs,
                running="RUNNING" in jobs,
            )
        )
    return SyncStatus(project_id=project_id, interval_seconds=interval, sources=items)


@router.post(
    "/v1/projects/{project_id}/sources/{kind}/{source_id}/reconcile",
    status_code=202,
    response_model=SyncJob,
    summary="Reconcile one source now (coalesced with a job already queued for it)",
    responses={404: PROBLEM, 409: PROBLEM},
)
def reconcile_now(
    request: Request,
    principal: CurrentPrincipal,
    project_id: uuid.UUID,
    kind: Annotated[Kind, Path()],
    source_id: uuid.UUID,
    payload: Annotated[ReconcileRequest | None, Body()] = None,
) -> JSONResponse:
    table, _ = TABLES[kind]
    with tenant_session(principal.tenant_id) as conn:
        access = authorize(conn, principal, request, "project.source.manage", project_id)
        if access.project["status"] == "ARCHIVED":
            raise conflict("PROJECT_ARCHIVED", "Archived projects are read-only; restore the project first")
        source = (
            conn.execute(
                select(table.c.status).where(
                    table.c.tenant_id == principal.tenant_id,
                    table.c.project_id == project_id,
                    table.c.id == source_id,
                )
            )
            .mappings()
            .first()
        )
        if source is None:
            raise ApiError(
                404, "SOURCE_NOT_FOUND", "Not found", "This source is not selected for the project"
            )
        if source["status"] != "ACTIVE":
            raise conflict("SOURCE_PAUSED", "This source is paused; check its connection first")
        job = queue.enqueue(
            conn,
            principal.tenant_id,
            project_id,
            kind,
            source_id,
            "manual",
            mode="full" if payload and payload.full else "incremental",
            hints={"requested_by": principal.user_id},
        )
    return JSONResponse(SyncJob.model_validate(job).model_dump(mode="json"), status_code=202)


@router.get(
    "/v1/projects/{project_id}/sources/jobs",
    response_model=list[SyncJob],
    summary="Recent sync jobs for this project (newest first)",
    responses={404: PROBLEM},
)
def list_jobs(
    request: Request,
    principal: CurrentPrincipal,
    project_id: uuid.UUID,
    limit: Annotated[int, Query(ge=1, le=100)] = 20,
) -> list[SyncJob]:
    with tenant_session(principal.tenant_id) as conn:
        authorize(conn, principal, request, "project.read", project_id)
        rows = conn.execute(
            select(sync_jobs)
            .where(sync_jobs.c.project_id == project_id)
            .order_by(sync_jobs.c.created_at.desc())
            .limit(limit)
        ).mappings()
        return [SyncJob.model_validate(dict(r)) for r in rows]
