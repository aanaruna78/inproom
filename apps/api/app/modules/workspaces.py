"""Workspaces: create and list (tenant-scoped)."""

import uuid
from datetime import datetime
from typing import Annotated

from fastapi import APIRouter, Body, Header, Query, Request
from psycopg.errors import UniqueViolation
from pydantic import BaseModel, Field
from sqlalchemy import Connection, insert, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.exc import IntegrityError

from app.api_errors import conflict
from app.audit import record
from app.auth import CurrentPrincipal, Principal
from app.authz import require
from app.db import tenant_session
from app.db_schema import outbox_events, tenants, workspaces
from app.idempotency import request_fingerprint, run_idempotent
from app.pagination import MAX_LIMIT, Page, apply_keyset, next_cursor

router = APIRouter(prefix="/v1/workspaces", tags=["workspaces"])

PROBLEM = {"description": "problem+json"}


class WorkspaceCreate(BaseModel):
    slug: str = Field(pattern=r"^[a-z][a-z0-9-]{1,39}$", examples=["platform"])
    name: str = Field(min_length=1, max_length=120, examples=["Platform engineering"])


class Workspace(BaseModel):
    id: uuid.UUID
    slug: str
    name: str
    created_by: str
    created_at: datetime


def ensure_tenant(conn: Connection, principal: Principal) -> None:
    """Local/dev auto-provisioning of the caller's tenant row (managed provisioning arrives with E13)."""
    conn.execute(
        pg_insert(tenants)
        .values(id=principal.tenant_id, name=str(principal.tenant_id))
        .on_conflict_do_nothing(index_elements=["id"])
    )


def is_unique_violation(exc: IntegrityError, constraint: str) -> bool:
    orig = exc.orig
    return isinstance(orig, UniqueViolation) and getattr(orig.diag, "constraint_name", None) == constraint


@router.post(
    "",
    status_code=201,
    response_model=Workspace,
    summary="Create a workspace",
    responses={403: PROBLEM, 409: PROBLEM, 422: PROBLEM},
)
def create_workspace(
    request: Request,
    principal: CurrentPrincipal,
    payload: Annotated[WorkspaceCreate, Body()],
    idempotency_key: Annotated[str | None, Header(alias="Idempotency-Key")] = None,
):
    require(principal, "tenant.workspace.create", request, target_type="workspace")
    body = payload.model_dump()

    def handler(conn: Connection) -> tuple[int, dict]:
        try:
            ensure_tenant(conn, principal)
            row = (
                conn.execute(
                    insert(workspaces)
                    .values(tenant_id=principal.tenant_id, created_by=principal.user_id, **body)
                    .returning(*workspaces.c)
                )
                .mappings()
                .one()
            )
            result = Workspace.model_validate(dict(row)).model_dump(mode="json")
            conn.execute(
                insert(outbox_events).values(
                    tenant_id=principal.tenant_id, type="workspace.created", payload=result
                )
            )
            record(
                conn,
                principal,
                "workspace.create",
                "workspace",
                str(row["id"]),
                trace_id=request.state.trace_id,
                payload=body,
            )
        except IntegrityError as exc:
            if is_unique_violation(exc, "uq_workspaces_tenant_slug"):
                raise conflict(
                    "WORKSPACE_SLUG_EXISTS", f"Workspace slug '{payload.slug}' already exists"
                ) from exc
            raise
        return 201, result

    fingerprint = request_fingerprint("POST", "/v1/workspaces", body)
    return run_idempotent(principal.tenant_id, idempotency_key, fingerprint, handler)


@router.get("", response_model=Page[Workspace], summary="List workspaces in the caller's tenant")
def list_workspaces(
    request: Request,
    principal: CurrentPrincipal,
    limit: Annotated[int, Query(ge=1, le=MAX_LIMIT)] = 50,
    cursor: str | None = None,
) -> Page[Workspace]:
    require(principal, "tenant.read", request, target_type="workspace")
    # Explicit tenant filter in addition to row-level security (defence in depth).
    base = select(workspaces).where(workspaces.c.tenant_id == principal.tenant_id)
    stmt = apply_keyset(base, workspaces.c.created_at, workspaces.c.id, cursor, limit)
    with tenant_session(principal.tenant_id) as conn:
        rows = [dict(r) for r in conn.execute(stmt).mappings()]
    page, nxt = next_cursor(rows, limit)
    return Page[Workspace](items=[Workspace.model_validate(r) for r in page], next_cursor=nxt)
