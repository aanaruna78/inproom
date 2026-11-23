"""Enforcement points. Every route calls exactly one of `require_tenant` (tenant-level actions) or
`authorize` (anything inside a project). Every denial is audited in the caller's tenant, in its own
transaction, so the audit survives the failing request."""

import uuid
from dataclasses import dataclass, field
from typing import Any

from fastapi import Request
from sqlalchemy import Connection, select

from app.audit import record_denied
from app.auth import Principal
from app.authz import policy
from app.db_schema import project_members, projects

NOT_ACCESSIBLE_REASON = "resource not found or not accessible"


class Forbidden(Exception):
    """403. `permission` is what the caller lacked; `code` is FORBIDDEN or SELF_APPROVAL_FORBIDDEN."""

    def __init__(self, permission: str, code: str = policy.FORBIDDEN, reason: str | None = None):
        super().__init__(permission)
        self.permission = permission
        self.code = code
        self.reason = reason or f"missing permission {permission}"


class NotFound(Exception):
    """One response for missing, other-tenant and not-a-member resources - no existence leak."""

    def __init__(self, resource: str):
        super().__init__(resource)
        self.resource = resource


def _trace(request: Request) -> str | None:
    return getattr(request.state, "trace_id", None)


def require_tenant(
    principal: Principal,
    permission: str,
    request: Request,
    *,
    target_type: str,
    target_id: str | None = None,
) -> None:
    """Tenant-level permission check (e.g. tenant.workspace.create). Raises an audited Forbidden."""
    if policy.has_tenant_permission(principal.roles, permission):
        return
    record_denied(
        principal,
        permission,
        target_type,
        target_id,
        trace_id=_trace(request),
        reason=f"missing permission {permission}",
    )
    raise Forbidden(permission)


# Backwards-compatible name used since INPROOM-18.
require = require_tenant


def deny_not_found(
    principal: Principal,
    request: Request,
    *,
    action: str,
    resource: str,
    target_type: str,
    target_id: str,
) -> NotFound:
    """Audit a denied access to a resource the caller cannot see, then return the masked 404 to raise.

    Records only what the caller sent (the requested id), never anything about another tenant; missing and
    foreign resources are audited identically so the audit trail cannot be used to probe for existence.
    """
    record_denied(
        principal, action, target_type, target_id, trace_id=_trace(request), reason=NOT_ACCESSIBLE_REASON
    )
    return NotFound(resource)


@dataclass(frozen=True)
class ProjectAccess:
    project: dict[str, Any]
    member_roles: frozenset[str]  # rows in project_members for the caller
    roles: frozenset[str]  # effective project roles (membership + tenant_admin => project_admin)
    permissions: frozenset[str] = field(default_factory=frozenset)
    approval_scopes: frozenset[str] = field(default_factory=frozenset)


def member_roles(
    conn: Connection, tenant_id: uuid.UUID, project_id: uuid.UUID, user_id: str
) -> frozenset[str]:
    stmt = select(project_members.c.role).where(
        project_members.c.tenant_id == tenant_id,
        project_members.c.project_id == project_id,
        project_members.c.user_id == user_id,
    )
    return frozenset(conn.execute(stmt).scalars())


def authorize(
    conn: Connection,
    principal: Principal,
    request: Request,
    permission: str,
    project_id: uuid.UUID,
    *,
    scope: str | None = None,
    proposer_id: str | None = None,
    lock: bool = False,
) -> ProjectAccess:
    """Load the project and the caller's membership inside `conn` (tenant-filtered, under RLS) and decide.

    - not visible (missing, other tenant, not a member)  -> DENIED audit + masked 404
    - visible but not permitted (role, approval scope)    -> DENIED audit + 403 FORBIDDEN
    - proposer deciding their own approval                 -> DENIED audit + 403 SELF_APPROVAL_FORBIDDEN
    `lock=True` takes FOR UPDATE on the project row so callers can serialize changes per project.
    """
    stmt = select(projects).where(projects.c.tenant_id == principal.tenant_id, projects.c.id == project_id)
    if lock:
        stmt = stmt.with_for_update()
    row = conn.execute(stmt).mappings().first()
    mine = member_roles(conn, principal.tenant_id, project_id, principal.user_id) if row else frozenset()
    if row is None or not policy.can_see_project(principal.roles, mine):
        raise deny_not_found(
            principal,
            request,
            action=permission,
            resource="Project",
            target_type="project",
            target_id=str(project_id),
        )
    decision = policy.decide(
        principal.roles,
        mine,
        permission,
        scope=scope,
        is_proposer=proposer_id is not None and proposer_id == principal.user_id,
    )
    if not decision.allowed:
        record_denied(
            principal,
            permission,
            "project",
            str(project_id),
            trace_id=_trace(request),
            reason=decision.reason or "denied",
            project_id=project_id,
        )
        raise Forbidden(permission, decision.code or policy.FORBIDDEN, decision.reason)
    return ProjectAccess(
        project=dict(row),
        member_roles=mine,
        roles=policy.effective_project_roles(principal.roles, mine),
        permissions=policy.project_permissions(principal.roles, mine),
        approval_scopes=policy.approval_scopes(principal.roles, mine),
    )
