"""Provenance API: inspect where cited passages came from (INPROOM-182 / story INPROOM-30).

Citations the caller may not see reveal nothing: outside the project they are NOT_FOUND (indistinguishable
from a citation that never existed); withheld items return only the reason. Refusals in a batch are audited
once per call (DENIED, with the count and reasons); the other citations still resolve.
"""

import logging
import uuid
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Body, Query, Request
from pydantic import BaseModel, Field, ValidationError
from sqlalchemy import select

from app import audit
from app.api_errors import ApiError
from app.auth import CurrentPrincipal
from app.authz import authorize
from app.content import provenance
from app.db import tenant_session
from app.db_schema import source_objects, source_revisions
from app.modules.content import _readable, access_reason

router = APIRouter(tags=["provenance"])
log = logging.getLogger("inproom.provenance")
PROBLEM = {"description": "problem+json"}
MAX_CITATIONS = 100


class Citation(BaseModel):
    kind: Literal["confluence", "jira", "git", "upload"]
    source_id: uuid.UUID
    external_id: str = Field(min_length=1, max_length=1000)
    seq: int = Field(ge=1)
    anchor: str | None = Field(default=None, min_length=1, max_length=300)


class ResolveRequest(BaseModel):
    # Items are validated one by one (INPROOM-185): a malformed citation is an INVALID_CITATION result, not a
    # failed batch. Each item has the Citation shape: {kind, source_id, external_id, seq, anchor?}.
    citations: list[dict[str, Any]] = Field(min_length=1, max_length=MAX_CITATIONS)


class CitationError(BaseModel):
    code: str
    detail: str
    reason: str | None = None


class Resolved(BaseModel):
    citation: dict[str, Any]
    record: dict[str, Any] | None = None
    error: CitationError | None = None


class ResolveResponse(BaseModel):
    results: list[Resolved]
    counts: dict[str, int]


def _resolve_one(conn, project_id, c: Citation) -> tuple[dict | None, CitationError | None]:
    obj = (
        conn.execute(
            select(source_objects).where(
                source_objects.c.project_id == project_id,
                source_objects.c.source_kind == c.kind,
                source_objects.c.source_id == c.source_id,
                source_objects.c.external_id == c.external_id,
            )
        )
        .mappings()
        .first()
    )
    rev = None
    if obj is not None:
        rev = (
            conn.execute(
                select(source_revisions).where(
                    source_revisions.c.object_id == obj["id"], source_revisions.c.seq == c.seq
                )
            )
            .mappings()
            .first()
        )
    if obj is None or rev is None:  # never says which part is missing: nothing to probe with
        return None, CitationError(code="NOT_FOUND", detail="No such citation in this project")
    obj, rev = dict(obj), dict(rev)
    refused = access_reason(conn, obj)
    if refused:
        return None, CitationError(
            code="CONTENT_WITHHELD", detail="This source is withheld", reason=refused[0]
        )
    try:
        return provenance.build(conn, obj, rev, c.anchor), None
    except provenance.AnchorNotFound:
        return None, CitationError(
            code="ANCHOR_NOT_FOUND", detail="The revision has no passage with this anchor"
        )


@router.post(
    "/v1/projects/{project_id}/provenance/resolve",
    response_model=ResolveResponse,
    summary="Provenance of up to 100 citations: source, revision, modified, trust, access, freshness",
    responses={404: PROBLEM, 422: PROBLEM},
)
def resolve(
    request: Request,
    principal: CurrentPrincipal,
    project_id: uuid.UUID,
    payload: Annotated[ResolveRequest, Body()],
) -> ResolveResponse:
    results: list[Resolved] = []
    counts = {"resolved": 0, "withheld": 0, "not_found": 0, "anchor_not_found": 0, "invalid_citation": 0}
    with tenant_session(principal.tenant_id) as conn:
        authorize(conn, principal, request, "project.read", project_id)
        for raw in payload.citations:
            try:
                c = Citation.model_validate(raw)
            except ValidationError as exc:
                fields = sorted({".".join(str(p) for p in e["loc"]) for e in exc.errors()})
                error = CitationError(code="INVALID_CITATION", detail=f"invalid fields: {', '.join(fields)}")
                results.append(Resolved(citation=raw, error=error))
                counts["invalid_citation"] += 1
                continue
            record, error = _resolve_one(conn, project_id, c)
            results.append(Resolved(citation=c.model_dump(mode="json"), record=record, error=error))
            key = "resolved" if error is None else error.code.lower().replace("content_", "")
            counts[key] = counts.get(key, 0) + 1
    refused = counts["withheld"] + counts["not_found"]
    if refused:
        reasons = sorted(
            {
                r.error.reason or r.error.code
                for r in results
                if r.error and r.error.code in ("CONTENT_WITHHELD", "NOT_FOUND")  # refusals only
            }
        )
        audit.record_standalone(
            principal,
            "provenance.resolve",
            "citations",
            None,
            outcome="DENIED",
            trace_id=getattr(request.state, "trace_id", None),
            reason=f"{refused} of {len(results)} citations refused: {', '.join(reasons)}"[:500],
            project_id=project_id,
        )
    log.info("provenance resolve project=%s %s", project_id, counts)
    return ResolveResponse(results=results, counts=counts)


@router.get(
    "/v1/projects/{project_id}/content/{kind}/{source_id}/{external_id:path}/revisions/{seq}/provenance",
    summary="Provenance of one revision, or of one passage of it (?anchor=)",
    responses={404: PROBLEM, 409: PROBLEM},
)
def revision_provenance(
    request: Request,
    principal: CurrentPrincipal,
    project_id: uuid.UUID,
    kind: Literal["confluence", "jira", "git", "upload"],
    source_id: uuid.UUID,
    external_id: str,
    seq: int,
    anchor: Annotated[str | None, Query(min_length=1, max_length=300)] = None,
) -> dict[str, Any]:
    with tenant_session(principal.tenant_id) as conn:
        authorize(conn, principal, request, "project.read", project_id)
        try:
            obj, rev = _readable(conn, principal, request, project_id, kind, source_id, external_id, seq)
        except ApiError as exc:
            if exc.code != "CONTENT_WITHHELD" or exc.extra.get("reason") not in ("PURGED", "NOT_STORED"):
                raise
            # metadata of a purged or oversized revision is still valid provenance
            from app.modules.content import _object, _revision

            obj = _object(conn, project_id, kind, source_id, external_id)
            rev = _revision(conn, obj, seq)
        try:
            return provenance.build(conn, obj, rev, anchor)
        except provenance.AnchorNotFound as exc:
            raise ApiError(
                404, "ANCHOR_NOT_FOUND", "Not found", "The revision has no passage with this anchor"
            ) from exc
