import logging
import uuid

from fastapi import APIRouter
from pydantic import BaseModel, Field
from sqlalchemy.exc import SQLAlchemyError

from app.auth import CurrentPrincipal
from app.authz import policy
from app.db import tenant_session
from app.modules.members import register_user

router = APIRouter(prefix="/v1", tags=["identity"])
log = logging.getLogger(__name__)


class Me(BaseModel):
    user_id: str
    tenant_id: uuid.UUID
    email: str | None
    roles: list[str] = Field(description="Tenant-level roles honoured from the token (tenant_admin, auditor)")


@router.get(
    "/me",
    response_model=Me,
    summary="The verified caller identity and tenant; registers the caller in the tenant user directory",
    responses={401: {"description": "Missing or invalid bearer token (problem+json)"}},
)
def me(principal: CurrentPrincipal) -> Me:
    # Signing in registers the caller in the tenant directory so admins can add them to projects. Identity
    # does not depend on it: if the directory write fails, /v1/me still answers and the next call retries.
    try:
        with tenant_session(principal.tenant_id) as conn:
            register_user(conn, principal)
    except SQLAlchemyError:
        log.warning("tenant directory registration failed for tenant %s", principal.tenant_id, exc_info=True)
    return Me(
        user_id=principal.user_id,
        tenant_id=principal.tenant_id,
        email=principal.email,
        roles=sorted(policy.tenant_roles(principal.roles)),
    )
