"""Content store read API: source objects, revision lineage and original bytes (INPROOM-158 / INPROOM-27).

Objects are addressed by their source identity (kind, project source id, provider id), revisions by seq.
Metadata and lineage stay visible to project readers whatever the object's state; the original bytes are only
served while the object is CURRENT, its source is ACTIVE and the revision's bytes are stored. Otherwise the
answer is 409 CONTENT_WITHHELD with the reason, and the refusal is audited (DENIED).
"""

import base64
import json
import uuid
from datetime import datetime
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Body, Query, Request, Response
from pydantic import BaseModel, Field
from sqlalchemy import Connection, Text, cast, select, tuple_

from app import audit
from app.api_errors import ApiError
from app.auth import CurrentPrincipal, Principal
from app.authz import authorize
from app.content import derivatives as derivatives_module
from app.content import store as blobs
from app.content.producers import PRODUCERS
from app.content.revisions import IN_SCOPE, revision_bytes
from app.db import tenant_session
from app.db_schema import (
    derivatives,
    project_confluence_sources,
    project_jira_sources,
    project_repositories,
    source_objects,
    source_revisions,
)

router = APIRouter(tags=["content"])
PROBLEM = {"description": "problem+json"}
Kind = Literal["confluence", "jira", "git", "upload"]
SOURCE_TABLES = {
    "confluence": project_confluence_sources,
    "jira": project_jira_sources,
    "git": project_repositories,
}


class RevisionSummary(BaseModel):
    seq: int
    provider_version: str | None
    content_sha256: str
    size_bytes: int
    media_type: str
    stored: bool
    fetched_at: datetime
    purged_at: datetime | None


class Revision(RevisionSummary):
    parent_seq: int | None
    acl: dict[str, Any]
    job_id: uuid.UUID | None


class SourceObject(BaseModel):
    id: uuid.UUID
    source_kind: str
    source_id: uuid.UUID
    external_id: str
    title: str
    uri: str | None
    status: str
    status_reason: str | None
    acl: dict[str, Any]
    revisions: int
    current: RevisionSummary | None
    first_seen_at: datetime
    last_seen_at: datetime
    deleted_at: datetime | None


class SourceObjectDetail(SourceObject):
    lineage: list[Revision]


class SourceObjectPage(BaseModel):
    items: list[SourceObject]
    next_cursor: str | None


def _summary(rev: dict[str, Any]) -> dict[str, Any]:
    return rev | {"stored": rev["blob_key"] is not None and rev["purged_at"] is None}


def _encode(value: list[str]) -> str:
    return base64.urlsafe_b64encode(json.dumps(value).encode()).decode().rstrip("=")


def _decode(cursor: str) -> list[str]:
    try:
        value = json.loads(base64.urlsafe_b64decode(cursor + "=" * (-len(cursor) % 4)))
        if isinstance(value, list) and len(value) == 3 and all(isinstance(v, str) for v in value):
            return value
    except ValueError:
        pass
    raise ApiError(400, "INVALID_CURSOR", "Invalid cursor", "cursor is malformed")


def _object(conn: Connection, project_id, kind: str, source_id, external_id: str) -> dict[str, Any]:
    row = (
        conn.execute(
            select(source_objects).where(
                source_objects.c.project_id == project_id,
                source_objects.c.source_kind == kind,
                source_objects.c.source_id == source_id,
                source_objects.c.external_id == external_id,
            )
        )
        .mappings()
        .first()
    )
    if row is None:
        raise ApiError(404, "CONTENT_NOT_FOUND", "Not found", "No stored content for this source item")
    return dict(row)


def _source_status(conn: Connection, obj: dict[str, Any]) -> str:
    table = SOURCE_TABLES.get(obj["source_kind"])
    if table is None:
        return "ACTIVE"
    status = conn.execute(
        select(table.c.status).where(table.c.id == obj["source_id"], table.c.project_id == obj["project_id"])
    ).scalar()
    return status or "REMOVED"


@router.get(
    "/v1/projects/{project_id}/content",
    response_model=SourceObjectPage,
    summary="Stored source items (pages, issues) with their current revision; filter by kind, source, status",
    responses={400: PROBLEM, 404: PROBLEM},
)
def list_objects(
    request: Request,
    principal: CurrentPrincipal,
    project_id: uuid.UUID,
    kind: Kind | None = None,
    source_id: uuid.UUID | None = None,
    status: Annotated[str | None, Query(pattern="^(CURRENT|RESTRICTED|EXCLUDED|DELETED|TOO_LARGE)$")] = None,
    limit: Annotated[int, Query(ge=1, le=200)] = 100,
    cursor: Annotated[str | None, Query(max_length=400)] = None,
) -> SourceObjectPage:
    o, r = source_objects, source_revisions
    stmt = (
        select(o, r.c.seq, r.c.provider_version, r.c.content_sha256, r.c.size_bytes, r.c.media_type)
        .add_columns(r.c.blob_key, r.c.fetched_at, r.c.purged_at)
        .select_from(o.outerjoin(r, r.c.id == o.c.current_revision_id))
        .where(o.c.project_id == project_id)
    )
    if kind:
        stmt = stmt.where(o.c.source_kind == kind)
    if source_id:
        stmt = stmt.where(o.c.source_id == source_id)
    if status:
        stmt = stmt.where(o.c.status == status)
    order = (o.c.source_kind, cast(o.c.source_id, Text), o.c.external_id)
    if cursor:
        stmt = stmt.where(tuple_(*order) > tuple_(*_decode(cursor)))
    stmt = stmt.order_by(*order).limit(limit + 1)
    with tenant_session(principal.tenant_id) as conn:
        authorize(conn, principal, request, "project.read", project_id)
        rows = [dict(x) for x in conn.execute(stmt).mappings()]
    more = len(rows) > limit
    rows = rows[:limit]
    items = []
    for row in rows:
        current = None
        if row["seq"] is not None:
            current = RevisionSummary.model_validate(_summary(row))
        items.append(SourceObject.model_validate(row | {"current": current}))
    nxt = None
    if more:
        last = rows[-1]
        nxt = _encode([last["source_kind"], str(last["source_id"]), last["external_id"]])
    return SourceObjectPage(items=items, next_cursor=nxt)


def _lineage(conn: Connection, obj: dict[str, Any]) -> list[dict[str, Any]]:
    revs = [
        dict(x)
        for x in conn.execute(
            select(source_revisions)
            .where(source_revisions.c.object_id == obj["id"])
            .order_by(source_revisions.c.seq)
        ).mappings()
    ]
    seq_of = {r["id"]: r["seq"] for r in revs}
    return [_summary(r) | {"parent_seq": seq_of.get(r["parent_revision_id"])} for r in revs]


@router.get(
    "/v1/projects/{project_id}/content/{kind}/{source_id}/{external_id}",
    response_model=SourceObjectDetail,
    summary="One stored source item: status, ACL and its full revision lineage",
    responses={404: PROBLEM},
)
def get_object(
    request: Request,
    principal: CurrentPrincipal,
    project_id: uuid.UUID,
    kind: Kind,
    source_id: uuid.UUID,
    external_id: str,
) -> SourceObjectDetail:
    with tenant_session(principal.tenant_id) as conn:
        authorize(conn, principal, request, "project.read", project_id)
        obj = _object(conn, project_id, kind, source_id, external_id)
        lineage = _lineage(conn, obj)
    current = next((r for r in lineage if r["id"] == obj["current_revision_id"]), None)
    return SourceObjectDetail.model_validate(obj | {"current": current, "lineage": lineage})


def _withhold(
    principal: Principal, request: Request, obj: dict[str, Any], seq: int, reason: str, detail: str
) -> ApiError:
    audit.record_standalone(
        principal,
        "content.read",
        "source_revision",
        f"{obj['source_kind']}/{obj['source_id']}/{obj['external_id']}@{seq}",
        outcome="DENIED",
        trace_id=getattr(request.state, "trace_id", None),
        reason=reason,
        project_id=obj["project_id"],
    )
    return ApiError(409, "CONTENT_WITHHELD", "Content withheld", detail, reason=reason)


@router.get(
    "/v1/projects/{project_id}/content/{kind}/{source_id}/{external_id}/revisions/{seq}/raw",
    summary="The original bytes of one revision (ETag = sha256); withheld unless the item is in scope",
    responses={
        200: {"content": {"application/octet-stream": {}}, "description": "the original bytes"},
        404: PROBLEM,
        409: PROBLEM,
    },
)
def get_raw(
    request: Request,
    principal: CurrentPrincipal,
    project_id: uuid.UUID,
    kind: Kind,
    source_id: uuid.UUID,
    external_id: str,
    seq: int,
) -> Response:
    with tenant_session(principal.tenant_id) as conn:
        authorize(conn, principal, request, "project.read", project_id)
        obj = _object(conn, project_id, kind, source_id, external_id)
        rev = (
            conn.execute(
                select(source_revisions).where(
                    source_revisions.c.object_id == obj["id"], source_revisions.c.seq == seq
                )
            )
            .mappings()
            .first()
        )
        if rev is None:
            raise ApiError(404, "REVISION_NOT_FOUND", "Not found", "No such revision of this item")
        rev = dict(rev)
        source_status = _source_status(conn, obj)
    reason = None
    if obj["status"] != IN_SCOPE:
        reason, detail = obj["status"], f"the item is {obj['status'].lower()}: {obj['status_reason'] or ''}"
    elif source_status != "ACTIVE":
        reason, detail = f"SOURCE_{source_status}", f"the item's source is {source_status.lower()}"
    elif rev["purged_at"] is not None:
        reason, detail = "PURGED", "this revision's content was removed by the retention policy"
    elif rev["blob_key"] is None:
        reason, detail = "NOT_STORED", "this revision's content was not stored (over the size limit)"
    if reason:
        raise _withhold(principal, request, obj, seq, reason, detail.rstrip(": "))
    try:
        data = revision_bytes(blobs.get_store(), rev)
    except blobs.ObjectStoreUnavailable as exc:
        raise ApiError(
            503,
            "OBJECTSTORE_UNAVAILABLE",
            "Content store unavailable",
            "The content store cannot be reached; retry shortly",
            headers={"Retry-After": "30"},
        ) from exc
    except (blobs.BlobMissing, blobs.BlobCorrupt) as exc:
        detail = f"the stored content is unusable ({exc.code}); run Reconcile now to fetch it again"
        raise _withhold(principal, request, obj, seq, exc.code, detail) from exc
    etag = f'"{rev["content_sha256"]}"'
    if request.headers.get("if-none-match") == etag:
        return Response(status_code=304, headers={"ETag": etag})
    return Response(
        content=data,
        media_type=rev["media_type"],
        headers={"ETag": etag, "Cache-Control": "private, no-store", "X-Content-Type-Options": "nosniff"},
    )


class Derivative(BaseModel):
    id: uuid.UUID
    kind: str
    producer: str
    producer_version: int
    status: str
    output_sha256: str | None
    size_bytes: int | None
    error: dict[str, Any] | None
    reason: str | None
    created_at: datetime
    built_at: datetime | None
    invalidated_at: datetime | None


class Derivatives(BaseModel):
    revision_seq: int
    content_sha256: str
    items: list[Derivative]


@router.get(
    "/v1/projects/{project_id}/content/{kind}/{source_id}/{external_id}/revisions/{seq}/derivatives",
    response_model=Derivatives,
    summary="Everything derived from one revision, with producer@version and status (including history)",
    responses={404: PROBLEM},
)
def get_derivatives(
    request: Request,
    principal: CurrentPrincipal,
    project_id: uuid.UUID,
    kind: Kind,
    source_id: uuid.UUID,
    external_id: str,
    seq: int,
) -> Derivatives:
    with tenant_session(principal.tenant_id) as conn:
        authorize(conn, principal, request, "project.read", project_id)
        obj = _object(conn, project_id, kind, source_id, external_id)
        rev = (
            conn.execute(
                select(source_revisions).where(
                    source_revisions.c.object_id == obj["id"], source_revisions.c.seq == seq
                )
            )
            .mappings()
            .first()
        )
        if rev is None:
            raise ApiError(404, "REVISION_NOT_FOUND", "Not found", "No such revision of this item")
        rows = conn.execute(
            select(derivatives)
            .where(derivatives.c.revision_id == rev["id"])
            .order_by(derivatives.c.producer, derivatives.c.created_at)
        ).mappings()
        items = [Derivative.model_validate(dict(x)) for x in rows]
    return Derivatives(revision_seq=seq, content_sha256=rev["content_sha256"], items=items)


class InvalidateRequest(BaseModel):
    kind: Kind
    source_id: uuid.UUID
    external_id: str | None = Field(
        default=None, max_length=200, description="One item; omit for the whole source"
    )
    producer: str | None = Field(default=None, max_length=64, description="One producer; omit for all")
    reason: str = Field(min_length=3, max_length=300)


class InvalidateResult(BaseModel):
    objects: int
    invalidated: int
    queued: int
    pending: int
    job_id: uuid.UUID | None


@router.post(
    "/v1/projects/{project_id}/content/invalidate",
    response_model=InvalidateResult,
    status_code=202,
    summary="Invalidate the current revisions' derivatives (one item or a whole source); queue a rebuild",
    responses={404: PROBLEM, 422: PROBLEM},
)
def post_invalidate(
    request: Request,
    principal: CurrentPrincipal,
    project_id: uuid.UUID,
    payload: Annotated[InvalidateRequest, Body()],
) -> InvalidateResult:
    if payload.producer is not None and payload.producer not in PRODUCERS:
        raise ApiError(422, "UNKNOWN_PRODUCER", "Unknown producer", f"known producers: {sorted(PRODUCERS)}")
    with tenant_session(principal.tenant_id) as conn:
        authorize(conn, principal, request, "project.source.manage", project_id, lock=True)
        if payload.external_id is not None:
            _object(conn, project_id, payload.kind, payload.source_id, payload.external_id)
        result = derivatives_module.invalidate(
            conn,
            principal.tenant_id,
            project_id,
            kind=payload.kind,
            source_id=payload.source_id,
            external_id=payload.external_id,
            producer=payload.producer,
            reason=payload.reason,
        )
        if result["objects"] == 0:
            raise ApiError(404, "CONTENT_NOT_FOUND", "Not found", "No stored content for this source")
        audit.record(
            conn,
            principal,
            "content.invalidate",
            "content_source",
            f"{payload.kind}/{payload.source_id}"
            + (f"/{payload.external_id}" if payload.external_id else ""),
            trace_id=getattr(request.state, "trace_id", None),
            payload=payload.model_dump(mode="json") | result,
            project_id=project_id,
        )
    return InvalidateResult.model_validate(result)
