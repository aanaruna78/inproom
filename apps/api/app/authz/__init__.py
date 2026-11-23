"""Authorization (INPROOM-20): `policy` holds the role matrix, `guard` enforces it on every route."""

from app.authz import policy
from app.authz.guard import (
    NOT_ACCESSIBLE_REASON,
    Forbidden,
    NotFound,
    ProjectAccess,
    authorize,
    deny_not_found,
    require,
    require_tenant,
)

__all__ = [
    "NOT_ACCESSIBLE_REASON",
    "Forbidden",
    "NotFound",
    "ProjectAccess",
    "authorize",
    "deny_not_found",
    "policy",
    "require",
    "require_tenant",
]
