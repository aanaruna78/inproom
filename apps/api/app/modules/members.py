"""Project membership, effective permissions and the tenant user directory (INPROOM-94 / story INPROOM-20).

Membership is the only source of project roles (see app.authz.policy). Every change:
- locks the project row first, so concurrent changes to one project are serialized and the
  "at least one project_admin" invariant holds under races;
- writes exactly one audit event and one outbox event in the same transaction, or nothing at all
  (an unchanged role set writes nothing, so a repeated PUT is a no-op);
- takes effect on the member's next request, because authorize() reads membership on every call.
"""

import base64
import json
import uuid
from datetime import datetime
from typing import Annotated, Literal

from fastapi import APIRouter, Body, Path, Query, Request, Response
from pydantic import BaseModel, Field
from sqlalchemy import Connection, and_, delete, func, insert, or_, select
from sqlalchemy.dialects.postgresql import insert as pg_insert

from app import audit
from app.api_errors import ApiError, conflict
from app.audit import record_denied
from app.auth import CurrentPrincipal, Principal
from app.authz import authorize, policy, require_tenant
from app.db import tenant_session
from app.db_schema import outbox_events, project_members, tenant_users
from app.modules.workspaces import ensure_tenant
from app.pagination import MAX_LIMIT, Page

router = APIRouter(tags=["members"])

PROBLEM = {"description": "problem+json"}
ProjectRole = Literal[
    "project_admin",
    "product_owner",
    "tech_lead",
    "developer",
    "reviewer",
    "qa_lead",
    "tester",
    "release_manager",
    "viewer",
]


class Member(BaseModel):
    user_id: str
    email: str | None
    display_name: str | None
    roles: list[str]
    added_by: str
    added_at: datetime


class MemberRoles(BaseModel):
    roles: list[ProjectRole] = Field(min_length=1, max_length=len(policy.PROJECT_ROLES))


class MyPermissions(BaseModel):
    project_id: uuid.UUID
    tenant_roles: list[str]
    member_roles: list[str] = Field(description="Roles held through project membership")
    roles: list[str] = Field(description="Effective project roles (tenant_admin acts as project_admin)")
    permissions: list[str]
    approval_scopes: list[str]


class DirectoryUser(BaseModel):
    user_id: str
    email: str | None
    display_name: str | None
    last_seen_at: datetime


# ---------------------------------------------------------------- directory


def register_user(conn: Connection, principal: Principal) -> None:
    """Upsert the caller into the tenant directory (called from GET /v1/me on sign-in)."""
    ensure_tenant(conn, principal)
    stmt = pg_insert(tenant_users).values(
        tenant_id=principal.tenant_id,
        user_id=principal.user_id,
        email=principal.email,
        display_name=principal.name,
    )
    conn.execute(
        stmt.on_conflict_do_update(
            index_elements=["tenant_id", "user_id"],
            set_={
                "email": stmt.excluded.email,
                "display_name": stmt.excluded.display_name,
                "last_seen_at": func.now(),
            },
        )
    )


def _encode(sort_key: str, user_id: str) -> str:
    return base64.urlsafe_b64encode(json.dumps([sort_key, user_id]).encode()).decode().rstrip("=")


def _decode(cursor: str) -> tuple[str, str]:
    try:
        sort_key, user_id = json.loads(base64.urlsafe_b64decode(cursor + "=" * (-len(cursor) % 4)))
        return str(sort_key), str(user_id)
    except (ValueError, TypeError) as exc:
        raise ApiError(400, "INVALID_CURSOR", "Invalid cursor", "cursor is malformed") from exc


@router.get(
    "/v1/users",
    response_model=Page[DirectoryUser],
    summary="Tenant user directory (users who have signed in), optional email prefix search",
)
def list_users(
    request: Request,
    principal: CurrentPrincipal,
    q: Annotated[str | None, Query(max_length=120, description="Email prefix, case-insensitive")] = None,
    limit: Annotated[int, Query(ge=1, le=MAX_LIMIT)] = 20,
    cursor: str | None = None,
) -> Page[DirectoryUser]:
    require_tenant(principal, "tenant.read", request, target_type="user")
    sort_key = func.lower(func.coalesce(tenant_users.c.email, ""))
    stmt = select(tenant_users, sort_key.label("sort_key")).where(
        tenant_users.c.tenant_id == principal.tenant_id
    )
    if q:
        escaped = q.lower().replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        stmt = stmt.where(sort_key.like(escaped + "%", escape="\\"))
    if cursor:
        after_key, after_id = _decode(cursor)
        stmt = stmt.where(
            or_(sort_key > after_key, and_(sort_key == after_key, tenant_users.c.user_id > after_id))
        )
    stmt = stmt.order_by(sort_key, tenant_users.c.user_id).limit(limit + 1)
    with tenant_session(principal.tenant_id) as conn:
        rows = [dict(r) for r in conn.execute(stmt).mappings()]
    nxt = _encode(rows[limit - 1]["sort_key"], rows[limit - 1]["user_id"]) if len(rows) > limit else None
    return Page[DirectoryUser](items=[DirectoryUser.model_validate(r) for r in rows[:limit]], next_cursor=nxt)


# ---------------------------------------------------------------- members


def _members(conn: Connection, tenant_id: uuid.UUID, project_id: uuid.UUID) -> list[Member]:
    stmt = (
        select(
            project_members.c.user_id,
            func.array_agg(project_members.c.role).label("roles"),
            func.min(project_members.c.added_by).label("added_by"),
            func.min(project_members.c.added_at).label("added_at"),
            func.max(tenant_users.c.email).label("email"),
            func.max(tenant_users.c.display_name).label("display_name"),
        )
        .select_from(
            project_members.outerjoin(
                tenant_users,
                and_(
                    tenant_users.c.tenant_id == project_members.c.tenant_id,
                    tenant_users.c.user_id == project_members.c.user_id,
                ),
            )
        )
        .where(project_members.c.tenant_id == tenant_id, project_members.c.project_id == project_id)
        .group_by(project_members.c.user_id)
        .order_by(func.min(project_members.c.added_at), project_members.c.user_id)
    )
    order = {r: i for i, r in enumerate(policy.PROJECT_ROLES)}
    return [
        Member.model_validate(dict(r) | {"roles": sorted(r["roles"], key=order.__getitem__)})
        for r in conn.execute(stmt).mappings()
    ]


def _roles_of(conn: Connection, tenant_id: uuid.UUID, project_id: uuid.UUID, user_id: str) -> set[str]:
    stmt = select(project_members.c.role).where(
        project_members.c.tenant_id == tenant_id,
        project_members.c.project_id == project_id,
        project_members.c.user_id == user_id,
    )
    return set(conn.execute(stmt).scalars())


def _other_admins(conn: Connection, tenant_id: uuid.UUID, project_id: uuid.UUID, user_id: str) -> int:
    stmt = (
        select(func.count())
        .select_from(project_members)
        .where(
            project_members.c.tenant_id == tenant_id,
            project_members.c.project_id == project_id,
            project_members.c.role == policy.PROJECT_ADMIN,
            project_members.c.user_id != user_id,
        )
    )
    return conn.execute(stmt).scalar_one()


def _guard_last_admin(
    conn: Connection, tenant_id, project_id, user_id: str, old: set[str], new: set[str]
) -> None:
    if policy.PROJECT_ADMIN in old and policy.PROJECT_ADMIN not in new:
        if _other_admins(conn, tenant_id, project_id, user_id) == 0:
            raise conflict(
                "LAST_PROJECT_ADMIN",
                "A project must keep at least one project_admin; add another admin before removing this one",
            )


def _ordered(roles: set[str]) -> list[str]:
    return [r for r in policy.PROJECT_ROLES if r in roles]


def _record_change(
    conn: Connection,
    principal: Principal,
    request: Request,
    project_id: uuid.UUID,
    user_id: str,
    action: str,
    old: set[str],
    new: set[str],
) -> None:
    change = {
        "project_id": str(project_id),
        "user_id": user_id,
        "old_roles": _ordered(old),
        "new_roles": _ordered(new),
    }
    audit.record(
        conn,
        principal,
        action,
        "project_member",
        user_id,
        trace_id=request.state.trace_id,
        payload=change,
        project_id=project_id,
    )
    conn.execute(
        insert(outbox_events).values(
            tenant_id=principal.tenant_id,
            type="project.member.changed",
            payload=change | {"action": action, "changed_by": principal.user_id},
        )
    )


@router.get(
    "/v1/projects/{project_id}/members",
    response_model=list[Member],
    summary="List project members and their roles",
    responses={404: PROBLEM},
)
def list_members(request: Request, principal: CurrentPrincipal, project_id: uuid.UUID) -> list[Member]:
    with tenant_session(principal.tenant_id) as conn:
        authorize(conn, principal, request, "project.read", project_id)
        return _members(conn, principal.tenant_id, project_id)


@router.put(
    "/v1/projects/{project_id}/members/{user_id}",
    response_model=Member,
    summary="Set a member's project roles (replaces the set; idempotent)",
    responses={403: PROBLEM, 404: PROBLEM, 409: PROBLEM, 422: PROBLEM},
)
def put_member(
    request: Request,
    principal: CurrentPrincipal,
    project_id: uuid.UUID,
    user_id: Annotated[str, Path(min_length=1, max_length=255)],
    payload: Annotated[MemberRoles, Body()],
) -> Member:
    new = set(payload.roles)
    with tenant_session(principal.tenant_id) as conn:
        authorize(conn, principal, request, "project.member.manage", project_id, lock=True)
        known = conn.execute(
            select(tenant_users.c.user_id).where(
                tenant_users.c.tenant_id == principal.tenant_id, tenant_users.c.user_id == user_id
            )
        ).first()
        if known is None:
            not_in_tenant = True
        else:
            not_in_tenant = False
            old = _roles_of(conn, principal.tenant_id, project_id, user_id)
            if old != new:
                _guard_last_admin(conn, principal.tenant_id, project_id, user_id, old, new)
                if old - new:
                    conn.execute(
                        delete(project_members).where(
                            project_members.c.tenant_id == principal.tenant_id,
                            project_members.c.project_id == project_id,
                            project_members.c.user_id == user_id,
                            project_members.c.role.in_(old - new),
                        )
                    )
                for role in _ordered(new - old):
                    conn.execute(
                        insert(project_members).values(
                            tenant_id=principal.tenant_id,
                            project_id=project_id,
                            user_id=user_id,
                            role=role,
                            added_by=principal.user_id,
                        )
                    )
                action = "project.member.add" if not old else "project.member.update"
                _record_change(conn, principal, request, project_id, user_id, action, old, new)
            member = next(m for m in _members(conn, principal.tenant_id, project_id) if m.user_id == user_id)
    if not_in_tenant:
        # Audited after the (empty) transaction: a grant to an unknown or foreign user was attempted.
        record_denied(
            principal,
            "project.member.manage",
            "project_member",
            user_id,
            trace_id=request.state.trace_id,
            reason="user not in tenant directory",
            project_id=project_id,
        )
        raise ApiError(
            404, "USER_NOT_IN_TENANT", "Not found", "No such user in this tenant; they must sign in first"
        )
    return member


@router.delete(
    "/v1/projects/{project_id}/members/{user_id}",
    status_code=204,
    summary="Remove a member from the project (all roles)",
    responses={403: PROBLEM, 404: PROBLEM, 409: PROBLEM},
)
def remove_member(
    request: Request, principal: CurrentPrincipal, project_id: uuid.UUID, user_id: str
) -> Response:
    with tenant_session(principal.tenant_id) as conn:
        authorize(conn, principal, request, "project.member.manage", project_id, lock=True)
        old = _roles_of(conn, principal.tenant_id, project_id, user_id)
        if not old:
            raise ApiError(404, "MEMBER_NOT_FOUND", "Not found", "This user is not a member of the project")
        _guard_last_admin(conn, principal.tenant_id, project_id, user_id, old, set())
        conn.execute(
            delete(project_members).where(
                project_members.c.tenant_id == principal.tenant_id,
                project_members.c.project_id == project_id,
                project_members.c.user_id == user_id,
            )
        )
        _record_change(conn, principal, request, project_id, user_id, "project.member.remove", old, set())
    return Response(status_code=204)


@router.get(
    "/v1/projects/{project_id}/permissions/me",
    response_model=MyPermissions,
    summary="The caller's effective roles, permissions and approval scopes on a project",
    responses={404: PROBLEM},
)
def my_permissions(request: Request, principal: CurrentPrincipal, project_id: uuid.UUID) -> MyPermissions:
    with tenant_session(principal.tenant_id) as conn:
        access = authorize(conn, principal, request, "project.read", project_id)
    return MyPermissions(
        project_id=project_id,
        tenant_roles=sorted(policy.tenant_roles(principal.roles)),
        member_roles=_ordered(set(access.member_roles)),
        roles=_ordered(set(access.roles)),
        permissions=sorted(access.permissions),
        approval_scopes=sorted(access.approval_scopes),
    )
