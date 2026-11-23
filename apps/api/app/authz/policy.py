"""INPROOM authorization policy (INPROOM-20). Pure data + pure functions - no I/O - so the whole matrix is
unit-testable and can be rendered to docs/security/rbac-matrix.md.

Two levels of roles:
- Tenant roles come from the signed token (`realm_access.roles`): tenant_admin, auditor. Every authenticated
  user is implicitly a tenant member. Any *project* role names in the token are ignored on purpose: project
  access is decided only by project_members rows, read from the database on every request, so removing or
  downgrading a member is effective on that person's next call.
- Project roles live in project_members (one row per user x role).
"""

from dataclasses import dataclass
from enum import StrEnum

# ---- roles -------------------------------------------------------------------------------------------

TENANT_ADMIN = "tenant_admin"
AUDITOR = "auditor"
TENANT_ROLES: tuple[str, ...] = (TENANT_ADMIN, AUDITOR)

PROJECT_ADMIN = "project_admin"
PROJECT_ROLES: tuple[str, ...] = (
    PROJECT_ADMIN,
    "product_owner",
    "tech_lead",
    "developer",
    "reviewer",
    "qa_lead",
    "tester",
    "release_manager",
    "viewer",
)


class Kind(StrEnum):
    READ = "read"
    PROPOSE = "propose"
    APPROVE = "approve"
    EXECUTE = "execute"
    ADMIN = "admin"


# ---- tenant-level permissions ------------------------------------------------------------------------

# Granted to every authenticated user of the tenant.
TENANT_MEMBER_PERMISSIONS = frozenset({"tenant.read"})
TENANT_ROLE_PERMISSIONS: dict[str, frozenset[str]] = {
    # tenant_admin holds every tenant-level permission (including ones added later, e.g. by E13).
    TENANT_ADMIN: frozenset({"*"}),
    AUDITOR: frozenset({"tenant.read", "audit.read"}),
}

# ---- project-level permissions -----------------------------------------------------------------------

PROJECT_PERMISSIONS: dict[str, Kind] = {
    "project.read": Kind.READ,
    "project.update": Kind.ADMIN,
    "project.archive": Kind.ADMIN,
    "project.member.manage": Kind.ADMIN,
    "project.settings.update": Kind.ADMIN,
    "audit.read": Kind.READ,
    "artifact.propose": Kind.PROPOSE,
    "artifact.review": Kind.PROPOSE,
    "approval.decide": Kind.APPROVE,
    "process.execute": Kind.EXECUTE,
}

_ALL_READERS = frozenset(PROJECT_ROLES)
PROJECT_MATRIX: dict[str, frozenset[str]] = {
    "project.read": _ALL_READERS,
    "project.update": frozenset({PROJECT_ADMIN}),
    "project.archive": frozenset({PROJECT_ADMIN}),
    "project.member.manage": frozenset({PROJECT_ADMIN}),
    "project.settings.update": frozenset({PROJECT_ADMIN}),
    "audit.read": frozenset({PROJECT_ADMIN}),
    "artifact.propose": frozenset(
        {PROJECT_ADMIN, "product_owner", "tech_lead", "developer", "qa_lead", "tester"}
    ),
    "artifact.review": frozenset(
        {
            PROJECT_ADMIN,
            "product_owner",
            "tech_lead",
            "developer",
            "reviewer",
            "qa_lead",
            "tester",
            "release_manager",
        }
    ),
    # Holding approval.decide is necessary but not sufficient: the approval scope must match too.
    "approval.decide": frozenset({PROJECT_ADMIN, "product_owner", "tech_lead", "qa_lead", "release_manager"}),
    "process.execute": frozenset(
        {PROJECT_ADMIN, "tech_lead", "developer", "qa_lead", "tester", "release_manager"}
    ),
}

# Approval scope -> project roles that may decide it. project_admin may decide every scope.
APPROVER_SCOPES: dict[str, frozenset[str]] = {
    "requirement": frozenset({"product_owner"}),
    "plan": frozenset({"tech_lead"}),
    "impact": frozenset({"tech_lead"}),
    "test": frozenset({"qa_lead"}),
    "release": frozenset({"release_manager"}),
}

# Tenant roles that act inside every project of the tenant without a membership row.
TENANT_ROLE_PROJECT_GRANTS: dict[str, frozenset[str]] = {
    TENANT_ADMIN: frozenset({PROJECT_ADMIN}),  # acts as project_admin everywhere
}
# Tenant roles with a fixed, read-only permission set on every project.
TENANT_ROLE_PROJECT_PERMISSIONS: dict[str, frozenset[str]] = {
    AUDITOR: frozenset({"project.read", "audit.read"}),
}
# Read-only cap (INPROOM-99): an auditor keeps only these permissions on every project, even when they also
# hold write-capable project roles through membership. Precedence: tenant_admin overrides the cap (a tenant
# admin can change their own roles anyway, so capping them would add no protection).
READ_ONLY_CAP: dict[str, frozenset[str]] = {
    AUDITOR: TENANT_ROLE_PROJECT_PERMISSIONS[AUDITOR],
}


def _cap(token_roles: frozenset[str]) -> frozenset[str] | None:
    roles = tenant_roles(token_roles)
    if TENANT_ADMIN in roles:
        return None
    caps = [READ_ONLY_CAP[r] for r in roles if r in READ_ONLY_CAP]
    return frozenset.intersection(*caps) if caps else None


# ---- decisions ---------------------------------------------------------------------------------------

FORBIDDEN = "FORBIDDEN"
SELF_APPROVAL_FORBIDDEN = "SELF_APPROVAL_FORBIDDEN"


@dataclass(frozen=True)
class Decision:
    allowed: bool
    code: str | None = None  # FORBIDDEN | SELF_APPROVAL_FORBIDDEN when denied
    reason: str | None = None


ALLOW = Decision(True)


def tenant_roles(token_roles: frozenset[str] | set[str]) -> frozenset[str]:
    """Only tenant-level roles are honoured from the token."""
    return frozenset(r for r in token_roles if r in TENANT_ROLES)


def tenant_permissions(token_roles: frozenset[str]) -> frozenset[str]:
    granted = set(TENANT_MEMBER_PERMISSIONS)
    for role in tenant_roles(token_roles):
        granted |= TENANT_ROLE_PERMISSIONS[role]
    return frozenset(granted)


def has_tenant_permission(token_roles: frozenset[str], permission: str) -> bool:
    granted = tenant_permissions(token_roles)
    return "*" in granted or permission in granted


def sees_every_project(token_roles: frozenset[str]) -> bool:
    roles = tenant_roles(token_roles)
    return bool(roles & (TENANT_ROLE_PROJECT_GRANTS.keys() | TENANT_ROLE_PROJECT_PERMISSIONS.keys()))


def effective_project_roles(token_roles: frozenset[str], member_roles: frozenset[str]) -> frozenset[str]:
    roles = {r for r in member_roles if r in PROJECT_ROLES}
    for role in tenant_roles(token_roles):
        roles |= TENANT_ROLE_PROJECT_GRANTS.get(role, frozenset())
    return frozenset(roles)


def can_see_project(token_roles: frozenset[str], member_roles: frozenset[str]) -> bool:
    return bool(effective_project_roles(token_roles, member_roles)) or sees_every_project(token_roles)


def project_permissions(token_roles: frozenset[str], member_roles: frozenset[str]) -> frozenset[str]:
    roles = effective_project_roles(token_roles, member_roles)
    granted = {perm for perm, allowed in PROJECT_MATRIX.items() if roles & allowed}
    for role in tenant_roles(token_roles):
        granted |= TENANT_ROLE_PROJECT_PERMISSIONS.get(role, frozenset())
    cap = _cap(token_roles)
    return frozenset(granted if cap is None else granted & cap)


def approval_scopes(token_roles: frozenset[str], member_roles: frozenset[str]) -> frozenset[str]:
    if "approval.decide" not in project_permissions(token_roles, member_roles):
        return frozenset()
    roles = effective_project_roles(token_roles, member_roles)
    if PROJECT_ADMIN in roles:
        return frozenset(APPROVER_SCOPES)
    return frozenset(scope for scope, allowed in APPROVER_SCOPES.items() if roles & allowed)


def decide(
    token_roles: frozenset[str],
    member_roles: frozenset[str],
    permission: str,
    *,
    scope: str | None = None,
    is_proposer: bool = False,
) -> Decision:
    """Decide a project-level permission for a caller who can already see the project."""
    if permission not in PROJECT_PERMISSIONS:
        raise ValueError(f"unknown project permission {permission!r}")
    if permission not in project_permissions(token_roles, member_roles):
        return Decision(False, FORBIDDEN, f"missing permission {permission}")
    if permission == "approval.decide":
        if scope not in APPROVER_SCOPES:
            raise ValueError(f"approval.decide needs a known scope, got {scope!r}")
        if scope not in approval_scopes(token_roles, member_roles):
            return Decision(False, FORBIDDEN, f"role cannot decide {scope} approvals")
        if is_proposer:
            return Decision(False, SELF_APPROVAL_FORBIDDEN, "the proposer cannot approve their own proposal")
    return ALLOW
