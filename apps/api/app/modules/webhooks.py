"""GitHub webhooks (INPROOM-110 / story INPROOM-22).

Registration is an external write: opt-in, only when INPROOM_PUBLIC_URL is set, find-before-create (so
retries never create a second hook), and audited by the selection change it belongs to. Each connection has
its own webhook secret in the vault.

The receiver is unauthenticated but every delivery must carry a valid X-Hub-Signature-256 HMAC (constant-time
compare). The X-GitHub-Delivery id is stored with a unique key, so a replayed delivery is acknowledged as a
duplicate and never processed twice. Only events for repositories/branches that a project selected produce
outbox events; everything else is acknowledged and ignored.
"""

import hashlib
import hmac
import json
import secrets
import uuid
from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from sqlalchemy import Connection, and_, insert, select
from sqlalchemy.dialects.postgresql import insert as pg_insert

from app import audit
from app.api_errors import ApiError
from app.auth import Principal
from app.config import get_settings
from app.connectors import vault
from app.connectors.base import GitProvider, ProviderError
from app.connectors.vault import Secret
from app.db import tenant_session
from app.db_schema import outbox_events, project_repositories, source_connections, webhook_deliveries

router = APIRouter(tags=["webhooks"])
MAX_BODY = 1_000_000
EVENTS = ["push", "create", "delete", "repository"]
WEBHOOK_ACTOR = "system:github-webhook"


def hook_url(tenant_id: uuid.UUID, connection_id: uuid.UUID) -> str | None:
    base = get_settings().public_url
    return f"{base.rstrip('/')}/v1/hooks/github/{tenant_id}/{connection_id}" if base else None


def webhook_secret(conn: Connection, tenant_id: uuid.UUID, connection_id: uuid.UUID) -> Secret:
    """The connection's webhook secret, created on first use."""
    existing = vault.load(conn, tenant_id, connection_id, "webhook")
    if existing is not None:
        return existing
    created = Secret(secrets.token_urlsafe(32))
    vault.store(conn, tenant_id, connection_id, "webhook", created)
    return created


def register(provider: GitProvider, repo_id: str, url: str, secret: Secret) -> tuple[str, str | None]:
    """Find-or-create our hook on the repository. Returns (webhook_status, webhook_id)."""
    try:
        hook_id = provider.find_hook(repo_id, url) or provider.create_hook(
            repo_id, url, secret.reveal(), EVENTS
        )
        return "registered", hook_id
    except ProviderError:
        # Typically the credential lacks the "Webhooks" permission. The selection still stands;
        # changes are picked up by reconciliation (INPROOM-25) instead.
        return "failed", None


# ---------------------------------------------------------------- receiver


def _verify(secret: Secret, body: bytes, signature: str | None) -> bool:
    if not signature or not signature.startswith("sha256="):
        return False
    expected = hmac.new(secret.reveal().encode(), body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, signature.removeprefix("sha256="))


def _target(event: str, payload: dict[str, Any]) -> tuple[str | None, str | None, str | None]:
    """(provider repo id, branch, head sha) the event is about."""
    repo_id = str((payload.get("repository") or {}).get("id") or "") or None
    branch = None
    if event == "push" and str(payload.get("ref", "")).startswith("refs/heads/"):
        branch = payload["ref"].removeprefix("refs/heads/")
    elif event in ("create", "delete") and payload.get("ref_type") == "branch":
        branch = payload.get("ref")
    return repo_id, branch, payload.get("after")


@router.post(
    "/v1/hooks/github/{tenant_id}/{connection_id}",
    status_code=202,
    summary="GitHub webhook receiver (HMAC-signed; replayed deliveries are acknowledged, not reprocessed)",
    responses={200: {"description": "duplicate delivery"}, 401: {}, 404: {}, 413: {}},
)
async def receive(request: Request, tenant_id: uuid.UUID, connection_id: uuid.UUID):
    if int(request.headers.get("content-length") or 0) > MAX_BODY:
        raise ApiError(413, "PAYLOAD_TOO_LARGE", "Payload too large", "Webhook payloads are limited to 1 MB")
    body = await request.body()
    if len(body) > MAX_BODY:
        raise ApiError(413, "PAYLOAD_TOO_LARGE", "Payload too large", "Webhook payloads are limited to 1 MB")
    system = Principal(user_id=WEBHOOK_ACTOR, tenant_id=tenant_id)
    trace = request.state.trace_id
    not_found = ApiError(404, "NOT_FOUND", "Not found", "Webhook endpoint not found")

    with tenant_session(tenant_id) as conn:
        row = (
            conn.execute(
                select(source_connections).where(
                    source_connections.c.tenant_id == tenant_id, source_connections.c.id == connection_id
                )
            )
            .mappings()
            .first()
        )
        secret = (
            vault.load(conn, tenant_id, connection_id, "webhook")
            if row and row["status"] != "REVOKED" and row["provider"] == "github"
            else None
        )
    if secret is None:
        raise not_found  # unknown tenant/connection, revoked, or no webhook configured: indistinguishable

    if not _verify(secret, body, request.headers.get("x-hub-signature-256")):
        audit.record_standalone(
            system,
            "webhook.receive",
            "connection",
            str(connection_id),
            outcome="DENIED",
            trace_id=trace,
            reason="invalid webhook signature",
        )
        raise ApiError(
            401, "WEBHOOK_SIGNATURE_INVALID", "Unauthorized", "Webhook signature is missing or invalid"
        )

    delivery_id = request.headers.get("x-github-delivery", "")
    event = request.headers.get("x-github-event", "")
    if not delivery_id or not event or len(delivery_id) > 100:
        raise ApiError(
            400, "WEBHOOK_HEADERS_MISSING", "Bad request", "X-GitHub-Delivery and X-GitHub-Event are required"
        )
    try:
        payload = json.loads(body or b"{}")
    except ValueError as exc:
        raise ApiError(400, "WEBHOOK_BODY_INVALID", "Bad request", "Webhook body is not JSON") from exc
    repo_id, branch, after = _target(event, payload)

    with tenant_session(tenant_id) as conn:
        matches = []
        if repo_id:
            matches = [
                dict(r)
                for r in conn.execute(
                    select(project_repositories).where(
                        and_(
                            project_repositories.c.tenant_id == tenant_id,
                            project_repositories.c.connection_id == connection_id,
                            project_repositories.c.provider_repo_id == repo_id,
                            project_repositories.c.status == "ACTIVE",
                        )
                    )
                ).mappings()
                if branch is None or branch in r["branches"]
            ]
        recorded = conn.execute(
            pg_insert(webhook_deliveries)
            .values(
                tenant_id=tenant_id,
                connection_id=connection_id,
                delivery_id=delivery_id,
                event=event,
                provider_repo_id=repo_id,
                outcome="processed" if matches else "ignored",
            )
            .on_conflict_do_nothing(index_elements=["connection_id", "delivery_id"])
            .returning(webhook_deliveries.c.delivery_id)
        ).first()
        if recorded is None:
            return JSONResponse({"status": "duplicate", "delivery_id": delivery_id}, status_code=200)
        for match in matches:
            conn.execute(
                insert(outbox_events).values(
                    tenant_id=tenant_id,
                    type="source.repository.changed",
                    payload={
                        "project_id": str(match["project_id"]),
                        "connection_id": str(connection_id),
                        "repository": match["full_name"],
                        "provider_repo_id": repo_id,
                        "event": event,
                        "branch": branch,
                        "after_sha": after,
                        "delivery_id": delivery_id,
                    },
                )
            )
    return {
        "status": "processed" if matches else "ignored",
        "delivery_id": delivery_id,
        "projects": len(matches),
    }
