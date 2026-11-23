"""Project source selection: which repositories and branches a project may use (INPROOM-109 / INPROOM-22).

Only what a project admin selected - and is still readable - is in the sync scope that indexing (E03)
consumes. Provider calls run outside the database transaction; the write then happens under the project lock.
"""

import uuid
from datetime import UTC, datetime
from typing import Annotated, Any

from fastapi import APIRouter, Body, Path, Query, Request, Response
from pydantic import AfterValidator, BaseModel, Field
from sqlalchemy import Connection, and_, delete, insert, select, update

from app import audit
from app.api_errors import ApiError, conflict
from app.auth import CurrentPrincipal, Principal
from app.authz import authorize
from app.connectors.registry import GitTransport, provider_errors, provider_for
from app.db import tenant_session
from app.db_schema import outbox_events, project_repositories, source_connections
from app.modules import settings as settings_module
from app.modules.connections import credential_of, load_connection

router = APIRouter(tags=["sources"])
PROBLEM = {"description": "problem+json"}


def _valid_branch(name: str) -> str:
    if ".." in name or name.startswith(("/", "-")) or name.endswith(("/", ".lock")) or "//" in name:
        raise ValueError("not a valid branch name")
    return name


BRANCH = Annotated[
    str, Field(min_length=1, max_length=255, pattern=r"^[A-Za-z0-9._/\-]+$"), AfterValidator(_valid_branch)
]


class RepositoryOption(BaseModel):
    id: str
    full_name: str
    private: bool
    default_branch: str
    permissions: dict[str, bool]
    excess_privileges: list[str]
    selected: bool


class RepositoryOptions(BaseModel):
    items: list[RepositoryOption]
    next_page: int | None


class SelectRepository(BaseModel):
    branches: list[BRANCH] = Field(default_factory=list, max_length=50, description="Empty = default branch")


class ProjectRepository(BaseModel):
    id: uuid.UUID
    connection_id: uuid.UUID
    connection_name: str
    connection_status: str
    provider_repo_id: str
    full_name: str
    private: bool
    default_branch: str
    branches: list[str]
    permissions: dict[str, bool]
    status: str
    status_reason: str | None
    webhook_status: str
    selected_by: str
    selected_at: datetime
    version: int


class ScopeItem(BaseModel):
    connection_id: uuid.UUID
    provider: str
    base_url: str
    provider_repo_id: str
    full_name: str
    branches: list[str]


class ScopeExclusion(BaseModel):
    full_name: str
    reason: str


class SyncScope(BaseModel):
    project_id: uuid.UUID
    repositories: list[ScopeItem]
    excluded: list[ScopeExclusion]


def _require_git_allowed(conn: Connection, principal: Principal, project: dict[str, Any]) -> None:
    current = settings_module.current_settings(conn, principal.tenant_id, project)
    if "git" not in current.settings.sources.allowed_kinds:
        raise conflict(
            "SOURCE_KIND_NOT_ALLOWED",
            "This project's settings do not allow git sources (sources.allowed_kinds)",
        )


def _selected(conn: Connection, tenant_id: uuid.UUID, project_id: uuid.UUID) -> list[dict[str, Any]]:
    j = project_repositories.join(
        source_connections,
        and_(
            source_connections.c.tenant_id == project_repositories.c.tenant_id,
            source_connections.c.id == project_repositories.c.connection_id,
        ),
    )
    stmt = (
        select(
            project_repositories,
            source_connections.c.display_name.label("connection_name"),
            source_connections.c.status.label("connection_status"),
            source_connections.c.provider,
            source_connections.c.base_url,
        )
        .select_from(j)
        .where(project_repositories.c.tenant_id == tenant_id, project_repositories.c.project_id == project_id)
        .order_by(project_repositories.c.full_name)
    )
    return [dict(r) for r in conn.execute(stmt).mappings()]


@router.get(
    "/v1/connections/{connection_id}/repositories",
    response_model=RepositoryOptions,
    summary="Repositories the connection's credential can see, for selecting into a project",
    responses={403: PROBLEM, 404: PROBLEM, 409: PROBLEM, 502: PROBLEM, 503: PROBLEM},
)
def list_repository_options(
    request: Request,
    principal: CurrentPrincipal,
    transport: GitTransport,
    connection_id: uuid.UUID,
    project_id: Annotated[uuid.UUID, Query(description="Project the repositories would be selected into")],
    q: Annotated[str | None, Query(max_length=100)] = None,
    page: Annotated[int, Query(ge=1, le=100)] = 1,
) -> RepositoryOptions:
    with tenant_session(principal.tenant_id) as conn:
        row = load_connection(conn, principal, request, connection_id, "project.source.manage")
        access = authorize(conn, principal, request, "project.source.manage", project_id)
        _require_git_allowed(conn, principal, access.project)
        token = credential_of(conn, principal, row)
        chosen = {
            r["provider_repo_id"]
            for r in _selected(conn, principal.tenant_id, project_id)
            if r["connection_id"] == connection_id
        }
    with provider_errors():
        repos, more = provider_for(row["provider"], row["base_url"], token, transport).list_repositories(
            q, page
        )
    items = [
        RepositoryOption(
            id=r.id,
            full_name=r.full_name,
            private=r.private,
            default_branch=r.default_branch,
            permissions=r.permissions,
            excess_privileges=r.excess_privileges,
            selected=r.id in chosen,
        )
        for r in repos
    ]
    return RepositoryOptions(items=items, next_page=page + 1 if more else None)


@router.get(
    "/v1/projects/{project_id}/sources/repositories",
    response_model=list[ProjectRepository],
    summary="Repositories selected for this project",
    responses={404: PROBLEM},
)
def list_project_repositories(request: Request, principal: CurrentPrincipal, project_id: uuid.UUID):
    with tenant_session(principal.tenant_id) as conn:
        authorize(conn, principal, request, "project.read", project_id)
        return [ProjectRepository.model_validate(r) for r in _selected(conn, principal.tenant_id, project_id)]


@router.put(
    "/v1/projects/{project_id}/sources/repositories/{connection_id}/{repo_id}",
    response_model=ProjectRepository,
    summary="Select a repository and its branches for this project (validated with the provider)",
    responses={403: PROBLEM, 404: PROBLEM, 409: PROBLEM, 422: PROBLEM, 502: PROBLEM, 503: PROBLEM},
)
def select_repository(
    request: Request,
    principal: CurrentPrincipal,
    transport: GitTransport,
    project_id: uuid.UUID,
    connection_id: uuid.UUID,
    repo_id: Annotated[str, Path(pattern=r"^\d{1,20}$")],
    payload: Annotated[SelectRepository, Body()],
) -> ProjectRepository:
    # 1) authorize and read what we need, then release the transaction before calling the provider
    with tenant_session(principal.tenant_id) as conn:
        access = authorize(conn, principal, request, "project.source.manage", project_id)
        if access.project["status"] == "ARCHIVED":
            raise conflict("PROJECT_ARCHIVED", "Archived projects are read-only; restore the project first")
        _require_git_allowed(conn, principal, access.project)
        row = load_connection(conn, principal, request, connection_id, "project.source.manage")
        if row["status"] not in ("ACTIVE", "DEGRADED"):
            raise conflict(
                "CONNECTION_NOT_ACTIVE", f"Connection is {row['status']}; check or rotate it first"
            )
        token = credential_of(conn, principal, row)
    # 2) validate with the provider: readable repository, existing branches
    provider = provider_for(row["provider"], row["base_url"], token, transport)
    with provider_errors(
        ("REPOSITORY_NOT_ACCESSIBLE", "The repository does not exist or the credential cannot read it")
    ):
        repo = provider.get_repository(repo_id)
        if not repo.can_read:
            raise ApiError(
                422, "REPOSITORY_NOT_ACCESSIBLE", "Unprocessable request", "The credential cannot read it"
            )
        branches = sorted(set(payload.branches)) or [repo.default_branch]
        missing = [b for b in branches if not provider.branch_exists(repo.id, b)]
    if missing:
        raise ApiError(
            422,
            "BRANCH_NOT_FOUND",
            "Unprocessable request",
            f"Branch not found: {', '.join(missing)}",
            branches=missing,
        )
    # 3) write under the project lock; unchanged selection is a no-op
    with tenant_session(principal.tenant_id) as conn:
        access = authorize(conn, principal, request, "project.source.manage", project_id, lock=True)
        if access.project["status"] == "ARCHIVED":
            raise conflict("PROJECT_ARCHIVED", "Archived projects are read-only; restore the project first")
        existing = (
            conn.execute(
                select(project_repositories).where(
                    project_repositories.c.tenant_id == principal.tenant_id,
                    project_repositories.c.project_id == project_id,
                    project_repositories.c.connection_id == connection_id,
                    project_repositories.c.provider_repo_id == repo.id,
                )
            )
            .mappings()
            .first()
        )
        values = {
            "full_name": repo.full_name,
            "private": repo.private,
            "default_branch": repo.default_branch,
            "branches": branches,
            "permissions": repo.permissions,
            "status": "ACTIVE",
            "status_reason": None,
        }
        if existing is None:
            conn.execute(
                insert(project_repositories).values(
                    tenant_id=principal.tenant_id,
                    project_id=project_id,
                    connection_id=connection_id,
                    provider_repo_id=repo.id,
                    selected_by=principal.user_id,
                    **values,
                )
            )
            action = "source.repository.selected"
        elif any(existing[k] != v for k, v in values.items()):
            conn.execute(
                update(project_repositories)
                .where(
                    project_repositories.c.tenant_id == principal.tenant_id,
                    project_repositories.c.id == existing["id"],
                )
                .values(**values, updated_at=datetime.now(UTC), version=project_repositories.c.version + 1)
            )
            action = "source.repository.updated"
        else:
            action = None  # identical selection: nothing written
        if action:
            change = {
                "project_id": str(project_id),
                "connection_id": str(connection_id),
                "repository": repo.full_name,
                "provider_repo_id": repo.id,
                "branches": branches,
                "excess_privileges": repo.excess_privileges,
            }
            audit.record(
                conn,
                principal,
                action,
                "project_repository",
                f"{connection_id}/{repo.id}",
                trace_id=request.state.trace_id,
                payload=change,
                project_id=project_id,
            )
            conn.execute(
                insert(outbox_events).values(tenant_id=principal.tenant_id, type=action, payload=change)
            )
        selected = next(
            r
            for r in _selected(conn, principal.tenant_id, project_id)
            if r["connection_id"] == connection_id and r["provider_repo_id"] == repo.id
        )
    return ProjectRepository.model_validate(selected)


@router.delete(
    "/v1/projects/{project_id}/sources/repositories/{connection_id}/{repo_id}",
    status_code=204,
    summary="Remove a repository from this project",
    responses={403: PROBLEM, 404: PROBLEM},
)
def remove_repository(
    request: Request,
    principal: CurrentPrincipal,
    project_id: uuid.UUID,
    connection_id: uuid.UUID,
    repo_id: Annotated[str, Path(pattern=r"^\d{1,20}$")],
) -> Response:
    with tenant_session(principal.tenant_id) as conn:
        authorize(conn, principal, request, "project.source.manage", project_id, lock=True)
        removed = conn.execute(
            delete(project_repositories)
            .where(
                project_repositories.c.tenant_id == principal.tenant_id,
                project_repositories.c.project_id == project_id,
                project_repositories.c.connection_id == connection_id,
                project_repositories.c.provider_repo_id == repo_id,
            )
            .returning(project_repositories.c.full_name)
        ).first()
        if removed is None:
            raise ApiError(
                404, "SOURCE_NOT_FOUND", "Not found", "This repository is not selected for the project"
            )
        change = {
            "project_id": str(project_id),
            "connection_id": str(connection_id),
            "repository": removed.full_name,
        }
        audit.record(
            conn,
            principal,
            "source.repository.removed",
            "project_repository",
            f"{connection_id}/{repo_id}",
            trace_id=request.state.trace_id,
            payload=change,
            project_id=project_id,
        )
        conn.execute(
            insert(outbox_events).values(
                tenant_id=principal.tenant_id, type="source.repository.removed", payload=change
            )
        )
    return Response(status_code=204)


@router.get(
    "/v1/projects/{project_id}/sources/scope",
    response_model=SyncScope,
    summary="The sync scope indexing may read: selected, readable repositories and branches only",
    responses={404: PROBLEM},
)
def sync_scope(request: Request, principal: CurrentPrincipal, project_id: uuid.UUID) -> SyncScope:
    with tenant_session(principal.tenant_id) as conn:
        authorize(conn, principal, request, "project.read", project_id)
        rows = _selected(conn, principal.tenant_id, project_id)
    included, excluded = [], []
    for r in rows:
        if r["status"] != "ACTIVE":
            excluded.append(ScopeExclusion(full_name=r["full_name"], reason=r["status_reason"] or "paused"))
        elif r["connection_status"] not in ("ACTIVE", "DEGRADED"):
            excluded.append(
                ScopeExclusion(full_name=r["full_name"], reason=f"connection {r['connection_status']}")
            )
        else:
            included.append(ScopeItem.model_validate(r))
    return SyncScope(project_id=project_id, repositories=included, excluded=excluded)
