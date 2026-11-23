"""Projects: create (atomic with owner, settings v1, outbox, audit), list, get, update, archive."""

import hashlib
import json
import re
import uuid
from datetime import datetime
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Body, Header, Query, Request, Response
from pydantic import BaseModel, Field
from sqlalchemy import Connection, exists, func, insert, select, update
from sqlalchemy.exc import IntegrityError

from app import audit
from app.api_errors import conflict, precondition_failed, precondition_required, unprocessable
from app.auth import CurrentPrincipal, Principal
from app.authz import ALL, ROLE_PERMISSIONS, deny_not_found, require
from app.config import get_settings
from app.db import tenant_session
from app.db_schema import outbox_events, project_members, project_settings_versions, projects, workspaces
from app.idempotency import request_fingerprint, run_idempotent
from app.modules.workspaces import is_unique_violation
from app.pagination import MAX_LIMIT, Page, apply_keyset, next_cursor

router = APIRouter(tags=["projects"])

PROBLEM = {"description": "problem+json"}
PROJECT_ADMIN_ROLE = "project_admin"


class ProjectCreate(BaseModel):
    key: str = Field(pattern=r"^[A-Z][A-Z0-9]{1,9}$", examples=["INPROOM"])
    name: str = Field(min_length=3, max_length=120, examples=["INPROOM platform"])
    description: str | None = Field(default=None, max_length=2000)
    mode: Literal["NEW", "MIGRATION"] = "NEW"
    process_package: str = Field(default="sdlc@1", pattern=r"^[a-z][a-z0-9-]*@\d+$")


class ProjectUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=3, max_length=120)
    description: str | None = Field(default=None, max_length=2000)


class Project(BaseModel):
    id: uuid.UUID
    workspace_id: uuid.UUID
    key: str
    name: str
    description: str | None
    mode: str
    status: str
    owner_id: str
    settings_version: int
    version: int
    created_at: datetime
    updated_at: datetime


class ProjectDetail(Project):
    settings: dict[str, Any]
    my_roles: list[str]


def default_settings(process_package: str) -> dict[str, Any]:
    return {
        "write_mode": "read_only",
        "approval_policy": "approval_required_for_external_writes",
        "model_policy": "tenant_default",
        "process_package": process_package,
    }


def settings_hash(settings: dict[str, Any]) -> str:
    canonical = json.dumps(settings, sort_keys=True, separators=(",", ":"))
    return "sha256:" + hashlib.sha256(canonical.encode()).hexdigest()


def is_tenant_admin(principal: Principal) -> bool:
    return any(ALL in ROLE_PERMISSIONS.get(role, frozenset()) for role in principal.roles)


def etag(version: int) -> str:
    return f'"{version}"'


def parse_if_match(value: str | None) -> int:
    if value is None:
        raise precondition_required("If-Match header with the current project version (ETag) is required")
    match = re.fullmatch(r'\s*(?:W/)?"?(\d+)"?\s*', value)
    if not match:
        raise precondition_failed("If-Match must be the ETag returned by the API")
    return int(match.group(1))


def _member_roles(conn: Connection, principal: Principal, project_id: uuid.UUID) -> list[str]:
    stmt = select(project_members.c.role).where(
        project_members.c.tenant_id == principal.tenant_id,
        project_members.c.project_id == project_id,
        project_members.c.user_id == principal.user_id,
    )
    return sorted(conn.execute(stmt).scalars())


def _load_visible_project(
    conn: Connection, principal: Principal, project_id: uuid.UUID, request: Request, action: str
) -> tuple[dict, list[str]]:
    """Return the project if the caller may see it: tenant admins see all, others need membership.
    Anything else (missing, other tenant, not a member) is the same 404 and is audited as DENIED."""
    stmt = select(projects).where(projects.c.tenant_id == principal.tenant_id, projects.c.id == project_id)
    row = conn.execute(stmt).mappings().first()
    roles = _member_roles(conn, principal, project_id) if row is not None else []
    if row is None or (not roles and not is_tenant_admin(principal)):
        raise deny_not_found(
            principal,
            request,
            action=action,
            resource="Project",
            target_type="project",
            target_id=str(project_id),
        )
    return dict(row), roles


def _require_project_admin(
    request: Request, principal: Principal, roles: list[str], project_id: uuid.UUID
) -> None:
    if is_tenant_admin(principal) or PROJECT_ADMIN_ROLE in roles:
        return
    # Re-use the guard so the denial is audited consistently.
    require(
        principal, f"project.admin:{project_id}", request, target_type="project", target_id=str(project_id)
    )


def _detail(conn: Connection, row: dict, roles: list[str]) -> dict[str, Any]:
    settings = conn.execute(
        select(project_settings_versions.c.settings).where(
            project_settings_versions.c.tenant_id == row["tenant_id"],
            project_settings_versions.c.project_id == row["id"],
            project_settings_versions.c.version == row["settings_version"],
        )
    ).scalar_one()
    return ProjectDetail.model_validate(row | {"settings": settings, "my_roles": roles}).model_dump(
        mode="json"
    )


@router.post(
    "/v1/workspaces/{workspace_id}/projects",
    status_code=201,
    response_model=ProjectDetail,
    summary="Create a NEW or MIGRATION project (owner, settings v1, audit and outbox in one transaction)",
    responses={403: PROBLEM, 404: PROBLEM, 409: PROBLEM, 422: PROBLEM},
)
def create_project(
    request: Request,
    principal: CurrentPrincipal,
    workspace_id: uuid.UUID,
    payload: Annotated[ProjectCreate, Body()],
    idempotency_key: Annotated[str | None, Header(alias="Idempotency-Key")] = None,
):
    require(
        principal, "workspace.project.create", request, target_type="workspace", target_id=str(workspace_id)
    )
    body = payload.model_dump()
    limit = get_settings().workspace_project_limit

    def handler(conn: Connection) -> tuple[int, dict]:
        try:
            # Lock the workspace row for the rest of this transaction: concurrent creates in the same
            # workspace queue here, so the count below always sees every previously committed project
            # and the active-project limit cannot be exceeded (INPROOM-92 / QA-03).
            locked = conn.execute(
                select(workspaces.c.id)
                .where(workspaces.c.tenant_id == principal.tenant_id, workspaces.c.id == workspace_id)
                .with_for_update()
            ).first()
            if locked is None:
                raise deny_not_found(
                    principal,
                    request,
                    action="workspace.project.create",
                    resource="Workspace",
                    target_type="workspace",
                    target_id=str(workspace_id),
                )
            active = conn.execute(
                select(func.count())
                .select_from(projects)
                .where(
                    projects.c.tenant_id == principal.tenant_id,
                    projects.c.workspace_id == workspace_id,
                    projects.c.status != "ARCHIVED",
                )
            ).scalar_one()
            if active >= limit:
                raise unprocessable("PROJECT_LIMIT_REACHED", f"Workspace already has {limit} active projects")

            status = "PENDING_SOURCES" if payload.mode == "MIGRATION" else "ACTIVE"
            row = (
                conn.execute(
                    insert(projects)
                    .values(
                        tenant_id=principal.tenant_id,
                        workspace_id=workspace_id,
                        key=payload.key,
                        name=payload.name,
                        description=payload.description,
                        mode=payload.mode,
                        status=status,
                        owner_id=principal.user_id,
                    )
                    .returning(*projects.c)
                )
                .mappings()
                .one()
            )
            project_id = row["id"]
            conn.execute(
                insert(project_members).values(
                    tenant_id=principal.tenant_id,
                    project_id=project_id,
                    user_id=principal.user_id,
                    role=PROJECT_ADMIN_ROLE,
                    added_by=principal.user_id,
                )
            )
            settings = default_settings(payload.process_package)
            conn.execute(
                insert(project_settings_versions).values(
                    tenant_id=principal.tenant_id,
                    project_id=project_id,
                    version=1,
                    settings=settings,
                    content_sha256=settings_hash(settings),
                    created_by=principal.user_id,
                )
            )
            result = _detail(conn, dict(row), [PROJECT_ADMIN_ROLE])
            conn.execute(
                insert(outbox_events).values(
                    tenant_id=principal.tenant_id, type="project.created", payload=result
                )
            )
            audit.record(
                conn,
                principal,
                "project.create",
                "project",
                str(project_id),
                trace_id=request.state.trace_id,
                payload=body,
                project_id=project_id,
            )
        except IntegrityError as exc:
            if is_unique_violation(exc, "uq_projects_tenant_key"):
                raise conflict("PROJECT_KEY_EXISTS", f"Project key '{payload.key}' already exists") from exc
            raise
        return 201, result

    fingerprint = request_fingerprint("POST", f"/v1/workspaces/{workspace_id}/projects", body)
    response = run_idempotent(principal.tenant_id, idempotency_key, fingerprint, handler)
    if response.status_code == 201:
        response.headers["ETag"] = etag(json.loads(response.body)["version"])
    return response


@router.get(
    "/v1/workspaces/{workspace_id}/projects",
    response_model=Page[Project],
    summary="List projects in a workspace (tenant admins see all; others see projects they belong to)",
    responses={404: PROBLEM},
)
def list_projects(
    request: Request,
    principal: CurrentPrincipal,
    workspace_id: uuid.UUID,
    limit: Annotated[int, Query(ge=1, le=MAX_LIMIT)] = 50,
    cursor: str | None = None,
) -> Page[Project]:
    require(principal, "project.read", request, target_type="workspace", target_id=str(workspace_id))
    stmt = select(projects).where(
        projects.c.tenant_id == principal.tenant_id, projects.c.workspace_id == workspace_id
    )
    if not is_tenant_admin(principal):
        stmt = stmt.where(
            exists().where(
                project_members.c.tenant_id == principal.tenant_id,
                project_members.c.project_id == projects.c.id,
                project_members.c.user_id == principal.user_id,
            )
        )
    stmt = apply_keyset(stmt, projects.c.created_at, projects.c.id, cursor, limit)
    with tenant_session(principal.tenant_id) as conn:
        if not conn.execute(
            select(
                exists().where(workspaces.c.tenant_id == principal.tenant_id, workspaces.c.id == workspace_id)
            )
        ).scalar():
            raise deny_not_found(
                principal,
                request,
                action="project.read",
                resource="Workspace",
                target_type="workspace",
                target_id=str(workspace_id),
            )
        rows = [dict(r) for r in conn.execute(stmt).mappings()]
    page, nxt = next_cursor(rows, limit)
    return Page[Project](items=[Project.model_validate(r) for r in page], next_cursor=nxt)


@router.get(
    "/v1/projects/{project_id}",
    response_model=ProjectDetail,
    summary="Get a project with its current settings",
    responses={404: PROBLEM},
)
def get_project(request: Request, response: Response, principal: CurrentPrincipal, project_id: uuid.UUID):
    require(principal, "project.read", request, target_type="project", target_id=str(project_id))
    with tenant_session(principal.tenant_id) as conn:
        row, roles = _load_visible_project(conn, principal, project_id, request, "project.read")
        result = _detail(conn, row, roles)
    response.headers["ETag"] = etag(row["version"])
    return result


@router.patch(
    "/v1/projects/{project_id}",
    response_model=ProjectDetail,
    summary="Update name/description (optimistic concurrency via If-Match)",
    responses={403: PROBLEM, 404: PROBLEM, 412: PROBLEM, 428: PROBLEM},
)
def update_project(
    request: Request,
    response: Response,
    principal: CurrentPrincipal,
    project_id: uuid.UUID,
    payload: Annotated[ProjectUpdate, Body()],
    if_match: Annotated[str | None, Header(alias="If-Match")] = None,
):
    changes = payload.model_dump(exclude_unset=True)
    if not changes:
        raise unprocessable("NO_CHANGES", "Provide name and/or description")
    if changes.get("name") is None and "name" in changes:
        raise unprocessable("NAME_REQUIRED", "name cannot be null")
    expected = parse_if_match(if_match)
    with tenant_session(principal.tenant_id) as conn:
        row, roles = _load_visible_project(conn, principal, project_id, request, "project.update")
        _require_project_admin(request, principal, roles, project_id)
        if row["status"] == "ARCHIVED":
            raise conflict("PROJECT_ARCHIVED", "Archived projects cannot be modified")
        updated = (
            conn.execute(
                update(projects)
                .where(
                    projects.c.tenant_id == principal.tenant_id,
                    projects.c.id == project_id,
                    projects.c.version == expected,
                )
                .values(**changes, version=projects.c.version + 1, updated_at=func.now())
                .returning(*projects.c)
            )
            .mappings()
            .first()
        )
        if updated is None:
            raise precondition_failed(f"Project was modified; current ETag is {etag(row['version'])}")
        audit.record(
            conn,
            principal,
            "project.update",
            "project",
            str(project_id),
            trace_id=request.state.trace_id,
            payload=changes,
            project_id=project_id,
        )
        result = _detail(conn, dict(updated), roles)
    response.headers["ETag"] = etag(updated["version"])
    return result


@router.post(
    "/v1/projects/{project_id}/archive",
    response_model=ProjectDetail,
    summary="Archive a project (idempotent)",
    responses={403: PROBLEM, 404: PROBLEM},
)
def archive_project(request: Request, response: Response, principal: CurrentPrincipal, project_id: uuid.UUID):
    with tenant_session(principal.tenant_id) as conn:
        row, roles = _load_visible_project(conn, principal, project_id, request, "project.archive")
        _require_project_admin(request, principal, roles, project_id)
        if row["status"] != "ARCHIVED":
            row = dict(
                conn.execute(
                    update(projects)
                    .where(projects.c.tenant_id == principal.tenant_id, projects.c.id == project_id)
                    .values(status="ARCHIVED", version=projects.c.version + 1, updated_at=func.now())
                    .returning(*projects.c)
                )
                .mappings()
                .one()
            )
            audit.record(
                conn,
                principal,
                "project.archive",
                "project",
                str(project_id),
                trace_id=request.state.trace_id,
                project_id=project_id,
            )
        result = _detail(conn, row, roles)
    response.headers["ETag"] = etag(row["version"])
    return result
