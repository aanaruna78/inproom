"""Permission guard. The static role map is temporary; INPROOM-20 replaces it with full RBAC behind the
same `require()` interface."""

from fastapi import Request

from app.audit import record_denied
from app.auth import Principal

ALL = "*"

ROLE_PERMISSIONS: dict[str, frozenset[str]] = {
    "tenant_admin": frozenset({ALL}),
    "project_admin": frozenset({"tenant.read", "project.read", "project.admin", "workspace.project.create"}),
    "product_owner": frozenset({"tenant.read", "project.read"}),
    "tech_lead": frozenset({"tenant.read", "project.read"}),
    "developer": frozenset({"tenant.read", "project.read"}),
    "reviewer": frozenset({"tenant.read", "project.read"}),
    "qa_lead": frozenset({"tenant.read", "project.read"}),
    "tester": frozenset({"tenant.read", "project.read"}),
    "release_manager": frozenset({"tenant.read", "project.read"}),
    "auditor": frozenset({"tenant.read", "project.read", "audit.read"}),
    "viewer": frozenset({"tenant.read", "project.read"}),
}


class Forbidden(Exception):
    def __init__(self, permission: str):
        super().__init__(permission)
        self.permission = permission


def has_permission(principal: Principal, permission: str) -> bool:
    for role in principal.roles:
        granted = ROLE_PERMISSIONS.get(role, frozenset())
        if ALL in granted or permission in granted:
            return True
    return False


def require(
    principal: Principal,
    permission: str,
    request: Request,
    *,
    target_type: str,
    target_id: str | None = None,
) -> None:
    """Allow or raise Forbidden (403). Every denial is audited in its own transaction."""
    if has_permission(principal, permission):
        return
    record_denied(
        principal,
        permission,
        target_type,
        target_id,
        trace_id=getattr(request.state, "trace_id", None),
        reason=f"missing permission {permission}",
    )
    raise Forbidden(permission)


class NotFound(Exception):
    """Same response for 'does not exist' and 'belongs to another tenant' - no existence leak."""

    def __init__(self, resource: str):
        super().__init__(resource)
        self.resource = resource


NOT_ACCESSIBLE_REASON = "resource not found or not accessible"


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

    The DENIED event is written in the caller's own tenant, in its own transaction (so it survives the
    failing request), and records only what the caller sent (the requested id) - never anything about
    the other tenant. Missing and foreign resources are audited identically, so the audit trail cannot
    be used to probe for existence either.
    """
    record_denied(
        principal,
        action,
        target_type,
        target_id,
        trace_id=getattr(request.state, "trace_id", None),
        reason=NOT_ACCESSIBLE_REASON,
    )
    return NotFound(resource)
