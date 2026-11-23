"""Append-only audit trail. The application role can only INSERT/SELECT audit_events (enforced in the DB)."""

import hashlib
import json
import uuid
from typing import Any

from sqlalchemy import Connection, insert

from app.auth import Principal
from app.db import tenant_session
from app.db_schema import audit_events


def payload_hash(payload: Any) -> str | None:
    """SHA-256 of the canonical JSON payload. Raw payloads are never stored in the audit trail."""
    if payload is None:
        return None
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)
    return "sha256:" + hashlib.sha256(canonical.encode()).hexdigest()


def _row(
    principal: Principal,
    action: str,
    target_type: str,
    target_id: str | None,
    outcome: str,
    trace_id: str | None,
    payload: Any,
    project_id: uuid.UUID | None,
    reason: str | None,
) -> dict[str, Any]:
    return {
        "tenant_id": principal.tenant_id,
        "project_id": project_id,
        "actor_id": principal.user_id,
        "action": action,
        "target_type": target_type,
        "target_id": target_id,
        "outcome": outcome,
        "reason": reason,
        "payload_sha256": payload_hash(payload),
        "trace_id": trace_id,
    }


def record(
    conn: Connection,
    principal: Principal,
    action: str,
    target_type: str,
    target_id: str | None = None,
    *,
    outcome: str = "SUCCESS",
    trace_id: str | None = None,
    payload: Any = None,
    project_id: uuid.UUID | None = None,
    reason: str | None = None,
) -> uuid.UUID:
    """Write an audit event inside the caller's transaction (commits or rolls back with the change)."""
    row = _row(principal, action, target_type, target_id, outcome, trace_id, payload, project_id, reason)
    return conn.execute(insert(audit_events).values(**row).returning(audit_events.c.id)).scalar_one()


def record_standalone(
    principal: Principal,
    action: str,
    target_type: str,
    target_id: str | None = None,
    *,
    outcome: str,
    trace_id: str | None = None,
    reason: str,
    project_id: uuid.UUID | None = None,
) -> uuid.UUID:
    """Write an audit event in its own transaction, so it survives the failing request's rollback."""
    with tenant_session(principal.tenant_id) as conn:
        return record(
            conn,
            principal,
            action,
            target_type,
            target_id,
            outcome=outcome,
            trace_id=trace_id,
            project_id=project_id,
            reason=reason,
        )


def record_denied(
    principal: Principal,
    action: str,
    target_type: str,
    target_id: str | None = None,
    *,
    trace_id: str | None = None,
    reason: str,
    project_id: uuid.UUID | None = None,
) -> uuid.UUID:
    """Write a DENIED event in its own transaction so it survives the failing request's rollback."""
    with tenant_session(principal.tenant_id) as conn:
        return record(
            conn,
            principal,
            action,
            target_type,
            target_id,
            outcome="DENIED",
            trace_id=trace_id,
            project_id=project_id,
            reason=reason,
        )
