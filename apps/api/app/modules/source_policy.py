"""Source trust and classification policy API (INPROOM-183 / story INPROOM-30)."""

import uuid
from datetime import UTC, datetime
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Body, Header, Request, Response
from pydantic import BaseModel, Field
from sqlalchemy import Connection, insert, select, update

from app import audit
from app.api_errors import ApiError, conflict, precondition_failed
from app.auth import CurrentPrincipal
from app.authz import authorize
from app.content import policies
from app.db import tenant_session
from app.db_schema import outbox_events, source_policies

router = APIRouter(tags=["sources"])
PROBLEM = {"description": "problem+json"}
Kind = Literal["confluence", "jira", "git", "upload"]


class Policy(BaseModel):
    kind: str
    source_id: uuid.UUID
    tier: str
    classification: str
    origin: Literal["default", "policy"]
    reason: str | None
    version: int
    updated_by: str | None
    updated_at: datetime | None
    project_max_classification: str
    within_project_cap: bool


class PolicyUpdate(BaseModel):
    tier: Literal["authoritative", "trusted", "reference", "untrusted"]
    classification: Literal["public", "internal", "confidential"]
    reason: str = Field(min_length=3, max_length=300)


def _check_source(conn: Connection, project_id: uuid.UUID, kind: str, source_id: uuid.UUID) -> None:
    if kind == "upload":
        if source_id != project_id:
            raise ApiError(
                404, "SOURCE_NOT_FOUND", "Not found", "Uploads use the project id as their source id"
            )
        return
    table = policies.SOURCE_TABLES[kind]
    found = conn.execute(
        select(table.c.id).where(table.c.id == source_id, table.c.project_id == project_id)
    ).first()
    if found is None:
        raise ApiError(404, "SOURCE_NOT_FOUND", "Not found", "No such source in this project")


def _view(conn: Connection, project_id, kind: str, source_id) -> Policy:
    p = policies.policy_of(conn, kind, source_id)
    cap = policies.project_cap(conn, project_id)
    return Policy(
        kind=kind,
        source_id=source_id,
        tier=p["tier"],
        classification=p["classification"],
        origin=p["origin"],
        reason=p["reason"],
        version=p["version"],
        updated_by=p["updated_by"],
        updated_at=p["updated_at"],
        project_max_classification=cap,
        within_project_cap=policies.RANK[p["classification"]] <= policies.RANK[cap],
    )


@router.get(
    "/v1/projects/{project_id}/sources/{kind}/{source_id}/policy",
    response_model=Policy,
    summary="A source's trust tier and classification (explicit policy or defaults) and the project cap",
    responses={404: PROBLEM},
)
def get_policy(
    request: Request,
    response: Response,
    principal: CurrentPrincipal,
    project_id: uuid.UUID,
    kind: Kind,
    source_id: uuid.UUID,
) -> Policy:
    with tenant_session(principal.tenant_id) as conn:
        authorize(conn, principal, request, "project.read", project_id)
        _check_source(conn, project_id, kind, source_id)
        view = _view(conn, project_id, kind, source_id)
    response.headers["ETag"] = f'"{view.version}"'
    return view


@router.put(
    "/v1/projects/{project_id}/sources/{kind}/{source_id}/policy",
    response_model=Policy,
    summary="Set a source's trust tier and classification (If-Match: the current version, 0 for defaults)",
    responses={404: PROBLEM, 409: PROBLEM, 412: PROBLEM, 422: PROBLEM, 428: PROBLEM},
)
def put_policy(
    request: Request,
    response: Response,
    principal: CurrentPrincipal,
    project_id: uuid.UUID,
    kind: Kind,
    source_id: uuid.UUID,
    payload: Annotated[PolicyUpdate, Body()],
    if_match: Annotated[str | None, Header(alias="If-Match")] = None,
) -> Policy:
    with tenant_session(principal.tenant_id) as conn:
        access = authorize(conn, principal, request, "project.source.manage", project_id, lock=True)
        if access.project["status"] == "ARCHIVED":
            raise conflict("PROJECT_ARCHIVED", "Archived projects are read-only; restore the project first")
        _check_source(conn, project_id, kind, source_id)
        current = policies.policy_of(conn, kind, source_id)
        if if_match is None:
            raise ApiError(
                428, "PRECONDITION_REQUIRED", "If-Match required", "Send If-Match with the current version"
            )
        if if_match.strip('"') != str(current["version"]):
            raise precondition_failed(f"The policy is at version {current['version']}")
        same = (current["origin"], current["tier"], current["classification"], current["reason"]) == (
            "policy",
            payload.tier,
            payload.classification,
            payload.reason,
        )
        if not same:  # identical updates change nothing (no new version, no event)
            now = datetime.now(UTC)
            values = {
                "trust": payload.tier,
                "classification": payload.classification,
                "reason": payload.reason,
                "updated_by": principal.user_id,
                "updated_at": now,
            }
            if current["origin"] == "default":
                conn.execute(
                    insert(source_policies).values(
                        tenant_id=principal.tenant_id,
                        project_id=project_id,
                        source_kind=kind,
                        source_id=source_id,
                        version=1,
                        **values,
                    )
                )
            else:
                conn.execute(
                    update(source_policies)
                    .where(source_policies.c.source_kind == kind, source_policies.c.source_id == source_id)
                    .values(version=source_policies.c.version + 1, **values)
                )
            change: dict[str, Any] = {
                "project_id": str(project_id),
                "source_kind": kind,
                "source_id": str(source_id),
                "from": {"tier": current["tier"], "classification": current["classification"]},
                "to": {"tier": payload.tier, "classification": payload.classification},
                "reason": payload.reason,
            }
            conn.execute(
                insert(outbox_events).values(
                    tenant_id=principal.tenant_id, type="source.policy.changed", payload=change
                )
            )
            audit.record(
                conn,
                principal,
                "source.policy.update",
                f"{kind}_source",
                str(source_id),
                trace_id=getattr(request.state, "trace_id", None),
                payload=change,
                project_id=project_id,
            )
        view = _view(conn, project_id, kind, source_id)
    response.headers["ETag"] = f'"{view.version}"'
    return view
