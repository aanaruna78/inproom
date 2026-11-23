"""Tenant-level Git provider connections (INPROOM-109 / story INPROOM-22).

The credential is validated with the provider BEFORE anything is stored, kept only in the vault, and never
returned, logged or audited - responses show the account, granted scopes and a fingerprint only.
Provider calls run outside database transactions, so no row lock is held while waiting on the network.
"""

import uuid
from datetime import UTC, datetime
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Body, Header, Request
from pydantic import BaseModel, Field, SecretStr
from sqlalchemy import Connection, insert, select, update
from sqlalchemy.exc import IntegrityError

from app import audit
from app.api_errors import ApiError, conflict
from app.auth import CurrentPrincipal, Principal
from app.authz import deny_not_found, require_tenant
from app.connectors import vault
from app.connectors.base import CredentialInvalid, ProviderError
from app.connectors.egress import EgressDenied, validate_base_url
from app.connectors.health import evaluate
from app.connectors.registry import GitTransport, provider_errors, provider_for
from app.connectors.vault import Secret
from app.db import tenant_session
from app.db_schema import outbox_events, project_repositories, source_connections
from app.idempotency import request_fingerprint, run_idempotent
from app.modules.workspaces import ensure_tenant, is_unique_violation

router = APIRouter(tags=["connections"])
PROBLEM = {"description": "problem+json"}


class ConnectionCreate(BaseModel):
    provider: Literal["github"] = "github"
    display_name: str = Field(min_length=1, max_length=80)
    base_url: str = Field(default="https://api.github.com", max_length=300)
    token: SecretStr = Field(min_length=20, max_length=400, description="Fine-grained token; never returned")


class CredentialRotate(BaseModel):
    token: SecretStr = Field(min_length=20, max_length=400)


class SourceConnection(BaseModel):
    id: uuid.UUID
    provider: str
    kind: str
    display_name: str
    base_url: str
    auth_type: str
    account_login: str | None
    granted_scopes: list[str]
    credential_fingerprint: str | None
    status: str
    health: dict[str, Any]
    last_checked_at: datetime | None
    created_by: str
    created_at: datetime
    updated_at: datetime
    version: int


# ---------------------------------------------------------------- helpers used by sources and webhooks


def load_connection(
    conn: Connection,
    principal: Principal,
    request: Request,
    connection_id: uuid.UUID,
    action: str,
    lock: bool = False,
) -> dict[str, Any]:
    stmt = select(source_connections).where(
        source_connections.c.tenant_id == principal.tenant_id, source_connections.c.id == connection_id
    )
    if lock:
        stmt = stmt.with_for_update()
    row = conn.execute(stmt).mappings().first()
    if row is None:
        raise deny_not_found(
            principal,
            request,
            action=action,
            resource="Connection",
            target_type="connection",
            target_id=str(connection_id),
        )
    return dict(row)


def credential_of(conn: Connection, principal: Principal, row: dict[str, Any]) -> Secret:
    if row["status"] == "REVOKED":
        raise conflict("CONNECTION_NOT_ACTIVE", "This connection was revoked; create a new connection")
    secret = vault.load(conn, principal.tenant_id, row["id"], "credential")
    if secret is None:
        raise conflict("CONNECTION_NOT_ACTIVE", "This connection has no stored credential")
    return secret


def _health(status: str, issues: list[dict[str, Any]], reachable: bool = True) -> dict[str, Any]:
    return {
        "status": {"ACTIVE": "HEALTHY", "DEGRADED": "DEGRADED", "INVALID": "INVALID"}.get(status, status),
        "reachable": reachable,
        "issues": issues,
        "checked_at": datetime.now(UTC).isoformat(),
    }


def _event(
    conn: Connection, principal: Principal, request: Request, action: str, row: dict, extra=None
) -> None:
    payload = {"connection_id": str(row["id"]), "provider": row["provider"], "status": row["status"]} | (
        extra or {}
    )
    audit.record(
        conn,
        principal,
        action,
        "connection",
        str(row["id"]),
        trace_id=request.state.trace_id,
        payload=payload,
    )
    conn.execute(insert(outbox_events).values(tenant_id=principal.tenant_id, type=action, payload=payload))


def _update(
    conn: Connection, principal: Principal, connection_id: uuid.UUID, **values: Any
) -> dict[str, Any]:
    return dict(
        conn.execute(
            update(source_connections)
            .where(
                source_connections.c.tenant_id == principal.tenant_id,
                source_connections.c.id == connection_id,
            )
            .values(**values, updated_at=datetime.now(UTC), version=source_connections.c.version + 1)
            .returning(*source_connections.c)
        )
        .mappings()
        .one()
    )


# ---------------------------------------------------------------- routes


@router.post(
    "/v1/connections",
    status_code=201,
    response_model=SourceConnection,
    summary="Connect a Git provider with a least-privilege token (validated before it is stored)",
    responses={403: PROBLEM, 409: PROBLEM, 422: PROBLEM, 502: PROBLEM, 503: PROBLEM},
)
def create_connection(
    request: Request,
    principal: CurrentPrincipal,
    transport: GitTransport,
    payload: Annotated[ConnectionCreate, Body()],
    idempotency_key: Annotated[str | None, Header(alias="Idempotency-Key")] = None,
):
    require_tenant(principal, "tenant.connection.manage", request, target_type="connection")
    try:
        base_url = validate_base_url(payload.base_url)
    except EgressDenied as exc:
        raise ApiError(422, "BASE_URL_NOT_ALLOWED", "Unprocessable request", str(exc)) from exc
    token = Secret(payload.token.get_secret_value())
    with provider_errors():
        account = provider_for(payload.provider, base_url, token, transport).whoami()

    def handler(conn: Connection) -> tuple[int, dict]:
        ensure_tenant(conn, principal)
        try:
            row = dict(
                conn.execute(
                    insert(source_connections)
                    .values(
                        tenant_id=principal.tenant_id,
                        provider=payload.provider,
                        kind="git",
                        display_name=payload.display_name,
                        base_url=base_url,
                        auth_type="token",
                        account_login=account.login,
                        granted_scopes=account.scopes,
                        credential_fingerprint=token.fingerprint(),
                        status="ACTIVE",
                        health=_health("ACTIVE", []),
                        last_checked_at=datetime.now(UTC),
                        created_by=principal.user_id,
                    )
                    .returning(*source_connections.c)
                )
                .mappings()
                .one()
            )
        except IntegrityError as exc:
            if is_unique_violation(exc, "uq_source_connections_name"):
                raise conflict(
                    "CONNECTION_NAME_EXISTS", f"A connection named '{payload.display_name}' exists"
                ) from exc
            raise
        vault.store(conn, principal.tenant_id, row["id"], "credential", token)
        _event(conn, principal, request, "connection.created", row, {"account": account.login})
        return 201, SourceConnection.model_validate(row).model_dump(mode="json")

    # The fingerprint covers what identifies the request, never the token itself.
    identity = {"provider": payload.provider, "display_name": payload.display_name, "base_url": base_url}
    fingerprint = request_fingerprint(
        "POST", "/v1/connections", identity | {"credential": token.fingerprint()}
    )
    return run_idempotent(principal.tenant_id, idempotency_key, fingerprint, handler)


@router.get("/v1/connections", response_model=list[SourceConnection], summary="Git provider connections")
def list_connections(request: Request, principal: CurrentPrincipal) -> list[SourceConnection]:
    require_tenant(principal, "tenant.read", request, target_type="connection")
    with tenant_session(principal.tenant_id) as conn:
        rows = conn.execute(
            select(source_connections)
            .where(source_connections.c.tenant_id == principal.tenant_id)
            .order_by(source_connections.c.created_at)
        ).mappings()
        return [SourceConnection.model_validate(dict(r)) for r in rows]


@router.get(
    "/v1/connections/{connection_id}",
    response_model=SourceConnection,
    summary="One connection with its latest health report",
    responses={404: PROBLEM},
)
def get_connection(
    request: Request, principal: CurrentPrincipal, connection_id: uuid.UUID
) -> SourceConnection:
    require_tenant(principal, "tenant.read", request, target_type="connection", target_id=str(connection_id))
    with tenant_session(principal.tenant_id) as conn:
        return SourceConnection.model_validate(
            load_connection(conn, principal, request, connection_id, "tenant.read")
        )


def check_credential(account_call) -> tuple[str, list[dict[str, Any]], bool, Any]:
    """Credential-level health. An outage is not proof of a bad credential, so it never flips the status."""
    try:
        return "ACTIVE", [], True, account_call()
    except CredentialInvalid:
        return (
            "INVALID",
            [{"code": "CREDENTIAL_INVALID", "message": "The provider rejects the credential"}],
            True,
            None,
        )
    except ProviderError as exc:
        return "UNKNOWN", [{"code": getattr(exc, "code", "PROVIDER_ERROR"), "message": str(exc)}], False, None


@router.post(
    "/v1/connections/{connection_id}/check",
    response_model=SourceConnection,
    summary="Run a health check now (credential validity and granted scopes)",
    responses={403: PROBLEM, 404: PROBLEM, 409: PROBLEM},
)
def check_connection(
    request: Request, principal: CurrentPrincipal, transport: GitTransport, connection_id: uuid.UUID
) -> SourceConnection:
    require_tenant(
        principal, "tenant.connection.manage", request, target_type="connection", target_id=str(connection_id)
    )
    with tenant_session(principal.tenant_id) as conn:
        row = load_connection(conn, principal, request, connection_id, "tenant.connection.manage")
        token = credential_of(conn, principal, row)
    provider = provider_for(row["provider"], row["base_url"], token, transport)

    status, issues, reachable, account = check_credential(provider.whoami)
    if status == "ACTIVE":
        status, repo_issues = evaluate(principal, row, provider)
        issues += repo_issues
    with tenant_session(principal.tenant_id) as conn:
        current = load_connection(
            conn, principal, request, connection_id, "tenant.connection.manage", lock=True
        )
        values: dict[str, Any] = {"last_checked_at": datetime.now(UTC)}
        if status != "UNKNOWN" and current["status"] != "REVOKED":
            values["status"] = status
        values["health"] = _health(values.get("status", current["status"]), issues, reachable)
        if account is not None:
            values |= {"account_login": account.login, "granted_scopes": account.scopes}
        updated = _update(conn, principal, connection_id, **values)
        _event(
            conn, principal, request, "connection.checked", updated, {"issues": [i["code"] for i in issues]}
        )
    return SourceConnection.model_validate(updated)


@router.put(
    "/v1/connections/{connection_id}/credential",
    response_model=SourceConnection,
    summary="Rotate the token (validated first; the old token is kept if the new one is rejected)",
    responses={403: PROBLEM, 404: PROBLEM, 409: PROBLEM, 422: PROBLEM},
)
def rotate_credential(
    request: Request,
    principal: CurrentPrincipal,
    transport: GitTransport,
    connection_id: uuid.UUID,
    payload: Annotated[CredentialRotate, Body()],
) -> SourceConnection:
    require_tenant(
        principal, "tenant.connection.manage", request, target_type="connection", target_id=str(connection_id)
    )
    with tenant_session(principal.tenant_id) as conn:
        row = load_connection(conn, principal, request, connection_id, "tenant.connection.manage")
    if row["status"] == "REVOKED":
        raise conflict("CONNECTION_NOT_ACTIVE", "This connection was revoked; create a new connection")
    token = Secret(payload.token.get_secret_value())
    with provider_errors():
        account = provider_for(row["provider"], row["base_url"], token, transport).whoami()
    with tenant_session(principal.tenant_id) as conn:
        load_connection(conn, principal, request, connection_id, "tenant.connection.manage", lock=True)
        vault.store(conn, principal.tenant_id, connection_id, "credential", token)
        updated = _update(
            conn,
            principal,
            connection_id,
            status="ACTIVE",
            account_login=account.login,
            granted_scopes=account.scopes,
            credential_fingerprint=token.fingerprint(),
            health=_health("ACTIVE", []),
            last_checked_at=datetime.now(UTC),
        )
        _event(conn, principal, request, "connection.rotated", updated, {"account": account.login})
    return SourceConnection.model_validate(updated)


@router.post(
    "/v1/connections/{connection_id}/revoke",
    response_model=SourceConnection,
    summary="Revoke: destroy the stored credential and pause every repository using this connection",
    responses={403: PROBLEM, 404: PROBLEM},
)
def revoke_connection(
    request: Request, principal: CurrentPrincipal, connection_id: uuid.UUID
) -> SourceConnection:
    require_tenant(
        principal, "tenant.connection.manage", request, target_type="connection", target_id=str(connection_id)
    )
    with tenant_session(principal.tenant_id) as conn:
        row = load_connection(conn, principal, request, connection_id, "tenant.connection.manage", lock=True)
        if row["status"] == "REVOKED":
            return SourceConnection.model_validate(row)  # idempotent
        vault.destroy(conn, principal.tenant_id, connection_id)
        conn.execute(
            update(project_repositories)
            .where(
                project_repositories.c.tenant_id == principal.tenant_id,
                project_repositories.c.connection_id == connection_id,
            )
            .values(status="PAUSED", status_reason="connection revoked", updated_at=datetime.now(UTC))
        )
        updated = _update(
            conn,
            principal,
            connection_id,
            status="REVOKED",
            credential_fingerprint=None,
            health=_health("REVOKED", [{"code": "REVOKED", "message": "Credential destroyed"}]),
        )
        _event(conn, principal, request, "connection.revoked", updated)
    return SourceConnection.model_validate(updated)
