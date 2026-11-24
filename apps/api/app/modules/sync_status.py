"""Source sync status, manual reconcile and job history (INPROOM-144 / story INPROOM-25).

Freshness per selected source comes from source_sync_state and the job queue:
- fresh    last success within 3 x the sync interval;
- stale    no success yet, or the last one is older than that;
- failing  the latest attempts failed (consecutive_failures > 0);
plus whether a job is queued or running. "Reconcile now" enqueues a coalesced manual job.
"""

import uuid
from datetime import UTC, datetime
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Body, Path, Query, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field
from sqlalchemy import Connection, insert, select, update

from app import audit
from app.api_errors import ApiError, conflict
from app.auth import CurrentPrincipal
from app.authz import authorize
from app.config import get_settings
from app.db import tenant_session
from app.db_schema import (
    outbox_events,
    project_confluence_sources,
    project_jira_sources,
    project_repositories,
    source_connections,
    source_sync_state,
    sync_jobs,
)
from app.sync import queue
from app.sync.errors import explain
from app.sync.freshness import freshness

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
    health: Literal["fresh", "stale", "failing", "never", "excluded", "paused"]
    pause_cause: str | None
    status_reason: str | None
    data_as_of: datetime | None
    last_success_at: datetime | None
    last_attempt_at: datetime | None
    next_due_at: datetime | None
    consecutive_failures: int
    last_error: dict[str, Any] | None  # code, message + title / explanation / action (INPROOM-145)
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
            select(
                table.c.id,
                table.c.status,
                table.c.status_reason,
                table.c.pause_cause,
                table.c[name].label("name"),
                source_connections.c.status.label("connection_status"),
            )
            .join(
                source_connections,
                (source_connections.c.tenant_id == table.c.tenant_id)
                & (source_connections.c.id == table.c.connection_id),
            )
            .where(table.c.tenant_id == tenant_id, table.c.project_id == project_id)
        ).all()
        out += [
            {
                "kind": kind,
                "source_id": r.id,
                "name": r.name,
                "source_status": r.status,
                "status_reason": r.status_reason,
                "pause_cause": r.pause_cause,
                "connection_status": r.connection_status,
            }
            for r in rows
        ]
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
    items = []
    for s in selected:
        state = states.get((s["kind"], s["source_id"]), {})
        failures = state.get("consecutive_failures", 0)
        last = state.get("last_success_at")
        health = freshness(s["source_status"], s["pause_cause"], s.pop("connection_status"), state)
        error = state.get("last_error")
        jobs = {j.status for j in active if (j.source_kind, j.source_id) == (s["kind"], s["source_id"])}
        items.append(
            SourceSyncStatus(
                **s,
                health=health,
                data_as_of=last,
                last_success_at=last,
                last_attempt_at=state.get("last_attempt_at"),
                next_due_at=state.get("next_due_at"),
                consecutive_failures=failures,
                last_error=(error | (explain(error.get("code")) or {})) if error else None,
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


# ---------------------------------------------------------------- exclude / include (INPROOM-145)


class SourceExclusion(BaseModel):
    kind: str
    source_id: uuid.UUID
    status: str
    pause_cause: str | None
    excluded_by: str | None
    excluded_at: datetime | None
    job_id: uuid.UUID | None = None


def _change(
    request: Request, principal, project_id: uuid.UUID, kind: str, source_id: uuid.UUID, exclude: bool
) -> SourceExclusion:
    table, name = TABLES[kind]
    with tenant_session(principal.tenant_id) as conn:
        access = authorize(conn, principal, request, "project.source.manage", project_id, lock=True)
        if access.project["status"] == "ARCHIVED":
            raise conflict("PROJECT_ARCHIVED", "Archived projects are read-only; restore the project first")
        row = (
            conn.execute(
                select(table).where(
                    table.c.tenant_id == principal.tenant_id,
                    table.c.project_id == project_id,
                    table.c.id == source_id,
                )
            )
            .mappings()
            .first()
        )
        if row is None:
            raise ApiError(
                404, "SOURCE_NOT_FOUND", "Not found", "This source is not selected for the project"
            )
        job = None
        already = (row["pause_cause"] == "excluded") == exclude
        if not already:
            now = datetime.now(UTC)
            if exclude:
                values = {
                    "status": "PAUSED",
                    "pause_cause": "excluded",
                    "status_reason": f"excluded by {principal.user_id}",
                    "excluded_by": principal.user_id,
                    "excluded_at": now,
                }
            else:
                values = {
                    "status": "ACTIVE",
                    "pause_cause": None,
                    "status_reason": None,
                    "excluded_by": None,
                    "excluded_at": None,
                }
            conn.execute(
                update(table)
                .where(table.c.tenant_id == principal.tenant_id, table.c.id == source_id)
                .values(**values, updated_at=now, version=table.c.version + 1)
            )
            action = "source.excluded" if exclude else "source.included"
            payload = {
                "project_id": str(project_id),
                "kind": kind,
                "source_id": str(source_id),
                "name": row[name],
            }
            audit.record(
                conn,
                principal,
                action,
                "source",
                str(source_id),
                trace_id=request.state.trace_id,
                payload=payload,
                project_id=project_id,
            )
            conn.execute(
                insert(outbox_events).values(tenant_id=principal.tenant_id, type=action, payload=payload)
            )
            if not exclude:  # catch up on whatever changed while it was excluded
                job = queue.enqueue(conn, principal.tenant_id, project_id, kind, source_id, "manual")
        current = (
            conn.execute(
                select(table).where(table.c.tenant_id == principal.tenant_id, table.c.id == source_id)
            )
            .mappings()
            .one()
        )
    return SourceExclusion(
        kind=kind,
        source_id=source_id,
        status=current["status"],
        pause_cause=current["pause_cause"],
        excluded_by=current["excluded_by"],
        excluded_at=current["excluded_at"],
        job_id=job["id"] if job else None,
    )


@router.post(
    "/v1/projects/{project_id}/sources/{kind}/{source_id}/exclude",
    response_model=SourceExclusion,
    summary="Exclude a source from the sync scope (history kept; only 'include' brings it back). Idempotent",
    responses={404: PROBLEM, 409: PROBLEM},
)
def exclude_source(
    request: Request,
    principal: CurrentPrincipal,
    project_id: uuid.UUID,
    kind: Annotated[Kind, Path()],
    source_id: uuid.UUID,
) -> SourceExclusion:
    return _change(request, principal, project_id, kind, source_id, exclude=True)


@router.post(
    "/v1/projects/{project_id}/sources/{kind}/{source_id}/include",
    response_model=SourceExclusion,
    summary="Include an excluded source again and queue a reconcile to catch up. Idempotent",
    responses={404: PROBLEM, 409: PROBLEM},
)
def include_source(
    request: Request,
    principal: CurrentPrincipal,
    project_id: uuid.UUID,
    kind: Annotated[Kind, Path()],
    source_id: uuid.UUID,
) -> SourceExclusion:
    return _change(request, principal, project_id, kind, source_id, exclude=False)
