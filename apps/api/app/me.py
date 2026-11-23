import uuid

from fastapi import APIRouter
from pydantic import BaseModel, Field

from app.auth import CurrentPrincipal
from app.authz import policy

router = APIRouter(prefix="/v1", tags=["identity"])


class Me(BaseModel):
    user_id: str
    tenant_id: uuid.UUID
    email: str | None
    roles: list[str] = Field(description="Tenant-level roles honoured from the token (tenant_admin, auditor)")


@router.get(
    "/me",
    response_model=Me,
    summary="The verified caller identity and tenant",
    responses={401: {"description": "Missing or invalid bearer token (problem+json)"}},
)
def me(principal: CurrentPrincipal) -> Me:
    return Me(
        user_id=principal.user_id,
        tenant_id=principal.tenant_id,
        email=principal.email,
        roles=sorted(policy.tenant_roles(principal.roles)),
    )
