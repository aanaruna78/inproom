"""Knowledge change feed API (INPROOM-186 / story INPROOM-31).

`GET /v1/projects/{id}/knowledge/changes?after=&limit=` returns what became unservable (`invalidated`),
servable again (`restored`) or got new content (`refreshed`), in order, from the cursor `after` (a seq).
Entries carry identifiers only: consumers that hold copies (indexes, caches) drop or rebuild by them. See
app/content/feed.py for the invalidation contract.
"""

import uuid
from datetime import datetime
from typing import Annotated, Literal

from fastapi import APIRouter, Body, Query, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field
from sqlalchemy import Connection, func, select

from app import audit
from app.api_errors import ApiError, conflict
from app.auth import CurrentPrincipal
from app.authz import authorize
from app.content import feed
from app.db import tenant_session
from app.db_schema import (
    code_snapshots,
    derivatives,
    knowledge_changes,
    project_confluence_sources,
    project_jira_sources,
    project_repositories,
    source_objects,
    sync_jobs,
)
from app.modules.sync_status import SyncJob

router = APIRouter(tags=["knowledge"])
PROBLEM = {"description": "problem+json"}
MAX_CHANGES = 500


class Change(BaseModel):
    seq: int
    at: datetime
    change: str  # invalidated | restored | refreshed
    scope: str  # item | source
    kind: str
    source_id: uuid.UUID
    external_id: str | None  # item scope only
    branch: str | None  # git source scope (a branch deselected or selected again)
    reason: str
    revision_seq: int | None  # refreshed: the new revision; PURGED: the purged revision


class ChangePage(BaseModel):
    items: list[Change]
    head: int  # the project's latest seq
    next: int | None  # pass as ?after= to continue; None when caught up


@router.get(
    "/v1/projects/{project_id}/knowledge/changes",
    response_model=ChangePage,
    summary="The project's ordered, gap-free feed of invalidated, restored and refreshed knowledge",
)
def list_changes(
    request: Request,
    principal: CurrentPrincipal,
    project_id: uuid.UUID,
    after: Annotated[int, Query(ge=0, description="Return entries with seq greater than this")] = 0,
    limit: Annotated[int, Query(ge=1, le=MAX_CHANGES)] = 100,
) -> ChangePage:
    with tenant_session(principal.tenant_id) as conn:
        authorize(conn, principal, request, "project.read", project_id)
        rows, head = feed.changes(conn, project_id, after, limit)
    items = [
        Change(
            seq=r["seq"],
            at=r["at"],
            change=r["change"],
            scope=r["scope"],
            kind=r["source_kind"],
            source_id=r["source_id"],
            external_id=r["external_id"],
            branch=r["branch"],
            reason=r["reason"],
            revision_seq=r["revision_seq"],
        )
        for r in rows
    ]
    last = items[-1].seq if items else after
    return ChangePage(items=items, head=head, next=last if last < head else None)


# ---------------------------------------------------------------- refresh and status (INPROOM-189)

SOURCES = {
    "confluence": project_confluence_sources,
    "jira": project_jira_sources,
    "git": project_repositories,
}


class RefreshRequest(BaseModel):
    scope: Literal["project", "source"] = "project"
    kind: Literal["confluence", "jira", "git"] | None = None
    source_id: uuid.UUID | None = None
    reason: str = Field(min_length=3, max_length=300)


class RefreshResult(BaseModel):
    queued: list[SyncJob]  # full reconciles, coalesced with jobs already queued for the same source
    skipped: list[dict[str, str]]  # paused sources: {kind, source_id, reason}


def _project_sources(conn: Connection, project_id: uuid.UUID) -> list[dict]:
    out = []
    for kind, table in SOURCES.items():
        name = (
            table.c.full_name
            if kind == "git"
            else (table.c.jira_key if kind == "jira" else table.c.space_key)
        )
        for r in conn.execute(
            select(table.c.id, table.c.status, name.label("name")).where(table.c.project_id == project_id)
        ).mappings():
            out.append({"kind": kind, "source_id": r["id"], "status": r["status"], "name": r["name"]})
    return sorted(out, key=lambda x: (x["kind"], x["name"] or ""))


@router.post(
    "/v1/projects/{project_id}/knowledge/refresh",
    status_code=202,
    response_model=RefreshResult,
    summary="Reconcile the project's sources (or one) in full now: deletions and permission changes included",
    responses={404: PROBLEM, 409: PROBLEM},
)
def refresh(
    request: Request,
    principal: CurrentPrincipal,
    project_id: uuid.UUID,
    payload: Annotated[RefreshRequest, Body()],
) -> JSONResponse:
    from app.sync import queue

    with tenant_session(principal.tenant_id) as conn:
        access = authorize(conn, principal, request, "project.source.manage", project_id)
        if access.project["status"] == "ARCHIVED":
            raise conflict("PROJECT_ARCHIVED", "Archived projects are read-only; restore the project first")
        targets = _project_sources(conn, project_id)
        if payload.scope == "source":
            if payload.kind is None or payload.source_id is None:
                raise ApiError(
                    422, "VALIDATION_FAILED", "Invalid request", "A source refresh needs kind and source_id"
                )
            targets = [t for t in targets if (t["kind"], t["source_id"]) == (payload.kind, payload.source_id)]
            if not targets:
                raise ApiError(
                    404, "SOURCE_NOT_FOUND", "Not found", "This source is not selected for the project"
                )
        queued, skipped = [], []
        for t in targets:
            if t["status"] != "ACTIVE":
                skipped.append(
                    {"kind": t["kind"], "source_id": str(t["source_id"]), "reason": "SOURCE_PAUSED"}
                )
                continue
            job = queue.enqueue(
                conn,
                principal.tenant_id,
                project_id,
                t["kind"],
                t["source_id"],
                "manual",
                mode="full",
                hints={"requested_by": principal.user_id, "knowledge_refresh": payload.reason},
            )
            queued.append(SyncJob.model_validate(job))
        audit.record(
            conn,
            principal,
            "knowledge.refresh",
            "project",
            str(project_id),
            trace_id=getattr(request.state, "trace_id", None),
            payload={
                "scope": payload.scope,
                "reason": payload.reason,
                "jobs": [str(j.id) for j in queued],
                "skipped": skipped,
            },
            project_id=project_id,
        )
    body = RefreshResult(queued=queued, skipped=skipped)
    return JSONResponse(body.model_dump(mode="json"), status_code=202)


class SourceStatus(BaseModel):
    kind: str
    source_id: uuid.UUID
    name: str | None
    status: str
    last_change_seq: int | None  # the latest feed entry about this source or its items
    last_change_at: datetime | None
    deletions_verified_at: datetime | None  # the last reconcile that saw the source's whole item set
    pending_derivatives: int  # derived outputs of current content still being built


class KnowledgeStatus(BaseModel):
    project_id: uuid.UUID
    head: int
    sources: list[SourceStatus]


def _verified_at(conn: Connection, kind: str, source_id: uuid.UUID) -> datetime | None:
    """When deletions were last verified: Confluence lists every page on every sync; Jira only on a full
    reconcile; git on every COMPLETE snapshot (any branch)."""
    if kind == "git":
        return conn.execute(
            select(func.max(code_snapshots.c.finished_at)).where(
                code_snapshots.c.source_id == source_id, code_snapshots.c.status == "COMPLETE"
            )
        ).scalar()
    stmt = select(func.max(sync_jobs.c.finished_at)).where(
        sync_jobs.c.source_kind == kind, sync_jobs.c.source_id == source_id, sync_jobs.c.status == "SUCCEEDED"
    )
    if kind == "jira":
        stmt = stmt.where(sync_jobs.c.result["full"].as_boolean().is_(True))
    job = conn.execute(stmt).scalar()
    if kind == "confluence":  # syncs run through the API (selection, sync now) record no job, only this
        api = conn.execute(
            select(project_confluence_sources.c.last_synced_at).where(
                project_confluence_sources.c.id == source_id
            )
        ).scalar()
        return max((t for t in (job, api) if t is not None), default=None)
    return job


@router.get(
    "/v1/projects/{project_id}/knowledge/status",
    response_model=KnowledgeStatus,
    summary="How current the project's knowledge is: feed head, per-source last change and deletion checks",
)
def status(request: Request, principal: CurrentPrincipal, project_id: uuid.UUID) -> KnowledgeStatus:
    k, o, d = knowledge_changes, source_objects, derivatives
    with tenant_session(principal.tenant_id) as conn:
        authorize(conn, principal, request, "project.read", project_id)
        head = conn.execute(
            select(func.coalesce(func.max(k.c.seq), 0)).where(k.c.project_id == project_id)
        ).scalar()
        last = {
            (r.source_kind, r.source_id): (r.seq, r.at)
            for r in conn.execute(
                select(
                    k.c.source_kind,
                    k.c.source_id,
                    func.max(k.c.seq).label("seq"),
                    func.max(k.c.at).label("at"),
                )
                .where(k.c.project_id == project_id)
                .group_by(k.c.source_kind, k.c.source_id)
            )
        }
        pending = {
            (r.source_kind, r.source_id): r.n
            for r in conn.execute(
                select(o.c.source_kind, o.c.source_id, func.count().label("n"))
                .select_from(d.join(o, o.c.current_revision_id == d.c.revision_id))
                .where(d.c.project_id == project_id, d.c.status == "PENDING", o.c.status == "CURRENT")
                .group_by(o.c.source_kind, o.c.source_id)
            )
        }
        sources = [
            SourceStatus(
                kind=t["kind"],
                source_id=t["source_id"],
                name=t["name"],
                status=t["status"],
                last_change_seq=last.get((t["kind"], t["source_id"]), (None, None))[0],
                last_change_at=last.get((t["kind"], t["source_id"]), (None, None))[1],
                deletions_verified_at=_verified_at(conn, t["kind"], t["source_id"]),
                pending_derivatives=pending.get((t["kind"], t["source_id"]), 0),
            )
            for t in _project_sources(conn, project_id)
        ]
    return KnowledgeStatus(project_id=project_id, head=head, sources=sources)
