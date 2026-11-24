"""Webhooks: GitHub registration (INPROOM-110) and signed receivers for GitHub, Jira and Confluence
(INPROOM-143 / story INPROOM-25).

Registration is an external write: opt-in, only when INPROOM_PUBLIC_URL is set, find-before-create (so
retries never create a second hook), and audited by the selection change it belongs to. Each connection has
its own webhook secret in the vault.

Receivers are unauthenticated but every delivery must carry a valid HMAC-SHA256 signature (constant-time
compare) made with the connection's webhook secret. The delivery id is stored with a unique key, so a replayed
delivery is acknowledged as a duplicate and never processed twice.

Webhooks are hints, not truth: a verified, new delivery only ENQUEUES a reconcile job (coalesced) for each
affected selected source. The job reads the provider's current state and emits only real changes against its
watermark, so duplicated, out-of-order and missed deliveries are all safe (app.sync).
"""

import hashlib
import hmac
import json
import secrets
import uuid
from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from sqlalchemy import Connection, and_, select
from sqlalchemy.dialects.postgresql import insert as pg_insert

from app import audit
from app.api_errors import ApiError
from app.auth import Principal
from app.config import get_settings
from app.connectors import vault
from app.connectors.base import GitProvider, ProviderError
from app.connectors.vault import Secret
from app.db import tenant_session
from app.db_schema import (
    project_confluence_sources,
    project_jira_sources,
    project_repositories,
    source_connections,
    webhook_deliveries,
)
from app.sync import queue

router = APIRouter(tags=["webhooks"])
MAX_BODY = 1_000_000
EVENTS = ["push", "create", "delete", "repository"]
WEBHOOK_ACTOR = "system:github-webhook"


def hook_path(provider: str, tenant_id: uuid.UUID, connection_id: uuid.UUID) -> str:
    return f"/v1/hooks/{provider}/{tenant_id}/{connection_id}"


def hook_url(tenant_id: uuid.UUID, connection_id: uuid.UUID, provider: str = "github") -> str | None:
    base = get_settings().public_url
    return f"{base.rstrip('/')}{hook_path(provider, tenant_id, connection_id)}" if base else None


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


# ---------------------------------------------------------------- receivers


def _verify(secret: Secret, body: bytes, signature: str | None) -> bool:
    if not signature or not signature.startswith("sha256="):
        return False
    expected = hmac.new(secret.reveal().encode(), body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, signature.removeprefix("sha256="))


def _target(event: str, payload: dict[str, Any]) -> tuple[str | None, str | None, str | None]:
    """(provider repo id, branch, head sha) a GitHub event is about."""
    repo_id = str((payload.get("repository") or {}).get("id") or "") or None
    branch = None
    if event == "push" and str(payload.get("ref", "")).startswith("refs/heads/"):
        branch = payload["ref"].removeprefix("refs/heads/")
    elif event in ("create", "delete") and payload.get("ref_type") == "branch":
        branch = payload.get("ref")
    return repo_id, branch, payload.get("after")


def _github_matches(
    conn: Connection, tenant_id, connection_id, event: str, payload: dict
) -> tuple[str | None, list, dict]:
    repo_id, branch, _ = _target(event, payload)
    if not repo_id:
        return None, [], {}
    rows = conn.execute(
        select(project_repositories).where(
            and_(
                project_repositories.c.tenant_id == tenant_id,
                project_repositories.c.connection_id == connection_id,
                project_repositories.c.provider_repo_id == repo_id,
                project_repositories.c.status == "ACTIVE",
            )
        )
    ).mappings()
    matches = [("git", dict(r)) for r in rows if branch is None or branch in r["branches"]]
    return repo_id, matches, {}


def _jira_matches(
    conn: Connection, tenant_id, connection_id, event: str, payload: dict
) -> tuple[str | None, list, dict]:
    issue = payload.get("issue") or {}
    project = (issue.get("fields") or {}).get("project") or payload.get("project") or {}
    project_id = str(project.get("id") or "") or None
    if not project_id:
        return None, [], {}
    rows = conn.execute(
        select(project_jira_sources).where(
            project_jira_sources.c.tenant_id == tenant_id,
            project_jira_sources.c.connection_id == connection_id,
            project_jira_sources.c.jira_project_id == project_id,
            project_jira_sources.c.status == "ACTIVE",
        )
    ).mappings()
    # A deletion can only be confirmed by a full id-set reconcile; everything else is incremental.
    mode = (
        "full" if event in ("jira:issue_deleted", "project_updated", "issuetype_updated") else "incremental"
    )
    return project_id, [("jira", dict(r)) for r in rows], {"mode": mode}


def _confluence_matches(
    conn: Connection, tenant_id, connection_id, event: str, payload: dict
) -> tuple[str | None, list, dict]:
    content = payload.get("page") or payload.get("blog") or payload.get("content") or {}
    space = (
        content.get("spaceKey")
        or (content.get("space") or {}).get("key")
        or (payload.get("space") or {}).get("key")
    )
    if not space:
        return None, [], {}
    rows = conn.execute(
        select(project_confluence_sources).where(
            project_confluence_sources.c.tenant_id == tenant_id,
            project_confluence_sources.c.connection_id == connection_id,
            project_confluence_sources.c.space_key == space,
            project_confluence_sources.c.status == "ACTIVE",
        )
    ).mappings()
    return space, [("confluence", dict(r)) for r in rows], {}


PROVIDERS = {
    # provider: (signature header, delivery-id header, event extractor, matcher)
    "github": (
        "x-hub-signature-256",
        "x-github-delivery",
        lambda h, p: h.get("x-github-event", ""),
        _github_matches,
    ),
    "jira": (
        "x-hub-signature",
        "x-atlassian-webhook-identifier",
        lambda h, p: str(p.get("webhookEvent", "")),
        _jira_matches,
    ),
    "confluence": (
        "x-hub-signature",
        "x-atlassian-webhook-identifier",
        lambda h, p: str(p.get("event") or p.get("webhookEvent") or ""),
        _confluence_matches,
    ),
}


async def _ingest(request: Request, provider: str, tenant_id: uuid.UUID, connection_id: uuid.UUID):
    if int(request.headers.get("content-length") or 0) > MAX_BODY:
        raise ApiError(413, "PAYLOAD_TOO_LARGE", "Payload too large", "Webhook payloads are limited to 1 MB")
    body = await request.body()
    if len(body) > MAX_BODY:
        raise ApiError(413, "PAYLOAD_TOO_LARGE", "Payload too large", "Webhook payloads are limited to 1 MB")
    signature_header, delivery_header, event_of, matcher = PROVIDERS[provider]
    system = Principal(user_id=f"system:{provider}-webhook", tenant_id=tenant_id)
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
            if row and row["status"] != "REVOKED" and row["provider"] == provider
            else None
        )
    if secret is None:
        raise not_found  # unknown tenant/connection, other provider, revoked, or no secret: indistinguishable

    if not _verify(secret, body, request.headers.get(signature_header)):
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
    try:
        payload = json.loads(body or b"{}")
    except ValueError as exc:
        raise ApiError(400, "WEBHOOK_BODY_INVALID", "Bad request", "Webhook body is not JSON") from exc
    if not isinstance(payload, dict):
        raise ApiError(400, "WEBHOOK_BODY_INVALID", "Bad request", "Webhook body must be a JSON object")
    event = event_of(request.headers, payload)
    # Atlassian may omit the identifier header (e.g. Data Center); then the signed body itself identifies it.
    delivery_id = request.headers.get(delivery_header) or (
        "sha256:" + hashlib.sha256(body).hexdigest() if provider != "github" else ""
    )
    if not delivery_id or not event or len(delivery_id) > 100:
        raise ApiError(
            400, "WEBHOOK_HEADERS_MISSING", "Bad request", "The delivery id and event are required"
        )

    with tenant_session(tenant_id) as conn:
        target, matches, options = matcher(conn, tenant_id, connection_id, event, payload)
        recorded = conn.execute(
            pg_insert(webhook_deliveries)
            .values(
                tenant_id=tenant_id,
                connection_id=connection_id,
                delivery_id=delivery_id,
                event=event[:100],
                provider_repo_id=target,
                outcome="processed" if matches else "ignored",
            )
            .on_conflict_do_nothing(index_elements=["connection_id", "delivery_id"])
            .returning(webhook_deliveries.c.delivery_id)
        ).first()
        if recorded is None:
            return JSONResponse({"status": "duplicate", "delivery_id": delivery_id}, status_code=200)
        jobs = [
            queue.enqueue(
                conn,
                tenant_id,
                source["project_id"],
                kind,
                source["id"],
                "webhook",
                mode=options.get("mode", "incremental"),
                hints={"last_delivery": delivery_id, "event": event[:100]},
            )
            for kind, source in matches
        ]
    return {
        "status": "processed" if matches else "ignored",
        "delivery_id": delivery_id,
        "projects": len(matches),
        "jobs": [str(j["id"]) for j in jobs],
    }


_RESPONSES = {200: {"description": "duplicate delivery"}, 401: {}, 404: {}, 413: {}}


@router.post(
    "/v1/hooks/github/{tenant_id}/{connection_id}",
    status_code=202,
    summary="GitHub webhook receiver (HMAC-signed; enqueues reconcile jobs; replays are acknowledged only)",
    responses=_RESPONSES,
)
async def receive(request: Request, tenant_id: uuid.UUID, connection_id: uuid.UUID):
    return await _ingest(request, "github", tenant_id, connection_id)


@router.post(
    "/v1/hooks/jira/{tenant_id}/{connection_id}",
    status_code=202,
    summary="Jira webhook receiver (X-Hub-Signature HMAC; enqueues reconcile jobs for the Jira project)",
    responses=_RESPONSES,
)
async def receive_jira(request: Request, tenant_id: uuid.UUID, connection_id: uuid.UUID):
    return await _ingest(request, "jira", tenant_id, connection_id)


@router.post(
    "/v1/hooks/confluence/{tenant_id}/{connection_id}",
    status_code=202,
    summary="Confluence webhook receiver (X-Hub-Signature HMAC; enqueues reconcile jobs for the space)",
    responses=_RESPONSES,
)
async def receive_confluence(request: Request, tenant_id: uuid.UUID, connection_id: uuid.UUID):
    return await _ingest(request, "confluence", tenant_id, connection_id)
