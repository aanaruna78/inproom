"""Tenant-level source connections: Git providers (INPROOM-109 / -22) and Jira (INPROOM-131 / -23).

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
from app.connectors.health import evaluate
from app.connectors.registry import Transports, jira_for, provider_errors, provider_for, validate_site
from app.connectors.vault import Secret
from app.db import tenant_session
from app.db_schema import outbox_events, project_jira_sources, project_repositories, source_connections
from app.idempotency import request_fingerprint, run_idempotent
from app.modules.workspaces import ensure_tenant, is_unique_violation

router = APIRouter(tags=["connections"])
PROBLEM = {"description": "problem+json"}


class ConnectionCreate(BaseModel):
    provider: Literal["github", "jira"] = "github"
    display_name: str = Field(min_length=1, max_length=80)
    base_url: str = Field(
        default="https://api.github.com",
        max_length=300,
        description="GitHub API URL, or the Jira site URL (e.g. https://acme.atlassian.net)",
    )
    email: str | None = Field(
        default=None,
        max_length=254,
        pattern=r"^[^@\s]+@[^@\s]+$",
        description="Jira Cloud: the Atlassian account email the API token belongs to. Omit for a Jira "
        "Data Center personal access token. Not used for GitHub.",
    )
    token: SecretStr = Field(
        min_length=20,
        max_length=400,
        description="Least-privilege token (GitHub fine-grained token, Jira "
        "API token or personal access token); never returned",
    )


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


KIND = {"github": "git", "jira": "jira"}


def auth_type_for(provider: str, email: str | None) -> str:
    if provider == "jira":
        return "basic_token" if email else "pat"
    return "token"


def adapter_for(row: dict[str, Any], token: Secret, transports: dict[str, Any]):
    """The provider adapter for a connection row (only `whoami` is shared by every provider)."""
    if row["provider"] == "jira":
        return jira_for(row, token, transports)
    return provider_for(row["provider"], row["base_url"], token, transports)


def _login(row: dict[str, Any], account: Any) -> str:
    """Jira Cloud authenticates as the email, so it stays the account; other providers report their login."""
    return row["account_login"] if row["auth_type"] == "basic_token" else account.login


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
    transports: Transports,
    payload: Annotated[ConnectionCreate, Body()],
    idempotency_key: Annotated[str | None, Header(alias="Idempotency-Key")] = None,
):
    require_tenant(principal, "tenant.connection.manage", request, target_type="connection")
    if payload.email and payload.provider != "jira":
        raise ApiError(422, "EMAIL_NOT_APPLICABLE", "Unprocessable request", "email is only used for Jira")
    base_url = validate_site(payload.provider, payload.base_url)
    token = Secret(payload.token.get_secret_value())
    draft = {
        "provider": payload.provider,
        "base_url": base_url,
        "auth_type": auth_type_for(payload.provider, payload.email),
        "account_login": payload.email,
    }
    with provider_errors():
        account = adapter_for(draft, token, transports).whoami()

    def handler(conn: Connection) -> tuple[int, dict]:
        ensure_tenant(conn, principal)
        try:
            row = dict(
                conn.execute(
                    insert(source_connections)
                    .values(
                        tenant_id=principal.tenant_id,
                        provider=payload.provider,
                        kind=KIND[payload.provider],
                        display_name=payload.display_name,
                        base_url=base_url,
                        auth_type=draft["auth_type"],
                        account_login=_login(draft, account),
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
    identity = {
        "provider": payload.provider,
        "display_name": payload.display_name,
        "base_url": base_url,
        "email": payload.email,
    }
    fingerprint = request_fingerprint(
        "POST", "/v1/connections", identity | {"credential": token.fingerprint()}
    )
    return run_idempotent(principal.tenant_id, idempotency_key, fingerprint, handler)


@router.get(
    "/v1/connections", response_model=list[SourceConnection], summary="Source connections (Git, Jira)"
)
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
    request: Request, principal: CurrentPrincipal, transports: Transports, connection_id: uuid.UUID
) -> SourceConnection:
    require_tenant(
        principal, "tenant.connection.manage", request, target_type="connection", target_id=str(connection_id)
    )
    with tenant_session(principal.tenant_id) as conn:
        row = load_connection(conn, principal, request, connection_id, "tenant.connection.manage")
        token = credential_of(conn, principal, row)
    provider = adapter_for(row, token, transports)

    status, issues, reachable, account = check_credential(provider.whoami)
    if status == "ACTIVE":
        if row["provider"] == "jira":
            from app.modules.jira_sources import evaluate_jira  # noqa: PLC0415 (avoids an import cycle)

            status, source_issues = evaluate_jira(principal, row, provider)
        else:
            status, source_issues = evaluate(principal, row, provider)
        issues += source_issues
    with tenant_session(principal.tenant_id) as conn:
        current = load_connection(
            conn, principal, request, connection_id, "tenant.connection.manage", lock=True
        )
        values: dict[str, Any] = {"last_checked_at": datetime.now(UTC)}
        if status != "UNKNOWN" and current["status"] != "REVOKED":
            values["status"] = status
        values["health"] = _health(values.get("status", current["status"]), issues, reachable)
        if account is not None:
            values |= {"account_login": _login(current, account), "granted_scopes": account.scopes}
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
    transports: Transports,
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
        account = adapter_for(row, token, transports).whoami()
    with tenant_session(principal.tenant_id) as conn:
        load_connection(conn, principal, request, connection_id, "tenant.connection.manage", lock=True)
        vault.store(conn, principal.tenant_id, connection_id, "credential", token)
        updated = _update(
            conn,
            principal,
            connection_id,
            status="ACTIVE",
            account_login=_login(row, account),
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
    summary="Revoke: destroy the stored credential and pause every source (repository or Jira project) on it",
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
        conn.execute(
            update(project_jira_sources)
            .where(
                project_jira_sources.c.tenant_id == principal.tenant_id,
                project_jira_sources.c.connection_id == connection_id,
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
