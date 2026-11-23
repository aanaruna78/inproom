"""Idempotency-Key support for POST endpoints (tenant-scoped, 24h retention).

Flow: claim the key in its own transaction (IN_PROGRESS) -> run the handler -> store the response
(COMPLETED). A retry with the same key and body replays the stored response; a different body is
rejected; a concurrent duplicate gets 409. If the handler fails, the claim is released so the client
can retry safely.
"""

import hashlib
import json
import uuid
from collections.abc import Callable
from datetime import timedelta
from typing import Any

from fastapi.responses import JSONResponse
from sqlalchemy import and_, delete, func, select, update
from sqlalchemy.dialects.postgresql import insert

from app.db import tenant_session
from app.db_schema import idempotency_keys

RETENTION = timedelta(hours=24)
REPLAY_HEADER = "Idempotent-Replayed"


class IdempotencyError(Exception):
    def __init__(self, status: int, code: str, detail: str):
        super().__init__(detail)
        self.status = status
        self.code = code
        self.detail = detail


def request_fingerprint(method: str, path: str, body: Any) -> str:
    canonical = json.dumps(
        {"m": method, "p": path, "b": body}, sort_keys=True, separators=(",", ":"), default=str
    )
    return hashlib.sha256(canonical.encode()).hexdigest()


def validate_key(key: str) -> None:
    if not 1 <= len(key) <= 128 or not key.isprintable():
        raise IdempotencyError(
            400, "IDEMPOTENCY_KEY_INVALID", "Idempotency-Key must be 1-128 printable characters"
        )


def _claim(tenant_id: uuid.UUID, key: str, fingerprint: str) -> JSONResponse | None:
    """Return a replayed response, raise on conflict, or None when this request now owns the key."""
    with tenant_session(tenant_id) as conn:
        conn.execute(
            delete(idempotency_keys).where(
                and_(
                    idempotency_keys.c.key == key,
                    idempotency_keys.c.created_at < func.now() - RETENTION,
                )
            )
        )
        claimed = conn.execute(
            insert(idempotency_keys)
            .values(tenant_id=tenant_id, key=key, request_sha256=fingerprint, status="IN_PROGRESS")
            .on_conflict_do_nothing(index_elements=["tenant_id", "key"])
            .returning(idempotency_keys.c.key)
        ).first()
        if claimed:
            return None
        row = conn.execute(select(idempotency_keys).where(idempotency_keys.c.key == key)).mappings().one()
    if row["request_sha256"] != fingerprint:
        raise IdempotencyError(
            422, "IDEMPOTENCY_KEY_REUSED", "Idempotency-Key was already used with a different request"
        )
    if row["status"] == "IN_PROGRESS":
        raise IdempotencyError(
            409, "IDEMPOTENCY_IN_PROGRESS", "A request with this Idempotency-Key is in progress"
        )
    return JSONResponse(
        row["response_body"], status_code=row["response_status"], headers={REPLAY_HEADER: "true"}
    )


def run_idempotent(
    tenant_id: uuid.UUID,
    key: str | None,
    fingerprint: str,
    handler: Callable[[], tuple[int, dict[str, Any]]],
) -> JSONResponse:
    """Execute `handler` at most once per (tenant, key). `handler` returns (status_code, json_body)."""
    if key is None:
        status, body = handler()
        return JSONResponse(body, status_code=status)
    validate_key(key)
    replay = _claim(tenant_id, key, fingerprint)
    if replay is not None:
        return replay
    try:
        status, body = handler()
    except BaseException:
        with tenant_session(tenant_id) as conn:
            conn.execute(delete(idempotency_keys).where(idempotency_keys.c.key == key))
        raise
    with tenant_session(tenant_id) as conn:
        conn.execute(
            update(idempotency_keys)
            .where(idempotency_keys.c.key == key)
            .values(status="COMPLETED", response_status=status, response_body=body)
        )
    return JSONResponse(body, status_code=status)
