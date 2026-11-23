"""Idempotency-Key support for POST endpoints (tenant-scoped, 24h retention).

Flow: claim the key in its own transaction (IN_PROGRESS), then run the handler AND store its response
(COMPLETED) in one transaction, so the business write and the replayable response commit together.
A retry with the same key and body replays the stored response; a different body is rejected (422); a
duplicate still in flight gets 409. If the handler fails, nothing is committed and the claim is released;
a claim abandoned by a crashed process is reclaimable after IN_PROGRESS_LEASE.
"""

import hashlib
import json
import logging
import uuid
from collections.abc import Callable
from datetime import timedelta
from typing import Any

from fastapi.responses import JSONResponse
from sqlalchemy import Connection, and_, delete, func, or_, select, update
from sqlalchemy.dialects.postgresql import insert

from app.db import tenant_session
from app.db_schema import idempotency_keys

logger = logging.getLogger(__name__)

RETENTION = timedelta(hours=24)
# A claim older than this with no committed result is treated as abandoned and may be reclaimed.
IN_PROGRESS_LEASE = timedelta(seconds=120)
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
                idempotency_keys.c.tenant_id == tenant_id,
                idempotency_keys.c.key == key,
                or_(
                    # Expired completed responses.
                    idempotency_keys.c.created_at < func.now() - RETENTION,
                    # Abandoned claims (process died between claim and commit). Because the response is
                    # stored in the same transaction as the business write, an IN_PROGRESS row can only
                    # mean nothing was committed - reclaiming it is safe.
                    and_(
                        idempotency_keys.c.status == "IN_PROGRESS",
                        idempotency_keys.c.created_at < func.now() - IN_PROGRESS_LEASE,
                    ),
                ),
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
        row = (
            conn.execute(
                select(idempotency_keys).where(
                    idempotency_keys.c.tenant_id == tenant_id, idempotency_keys.c.key == key
                )
            )
            .mappings()
            .one()
        )
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


def _release(tenant_id: uuid.UUID, key: str) -> None:
    """Best effort: free the claim after a failed attempt. If this fails too, the lease expires it."""
    try:
        with tenant_session(tenant_id) as conn:
            conn.execute(
                delete(idempotency_keys).where(
                    idempotency_keys.c.tenant_id == tenant_id,
                    idempotency_keys.c.key == key,
                    idempotency_keys.c.status == "IN_PROGRESS",
                )
            )
    except Exception:  # noqa: BLE001 - never mask the original failure
        logger.exception("could not release idempotency key; it will expire after the lease")


Handler = Callable[[Connection], tuple[int, dict[str, Any]]]


def run_idempotent(tenant_id: uuid.UUID, key: str | None, fingerprint: str, handler: Handler) -> JSONResponse:
    """Execute `handler(conn)` at most once per (tenant, key); it returns (status_code, json_body).

    The handler runs inside the tenant transaction opened here, and the response is stored in the
    SAME transaction. The business write and its replayable response therefore commit together: if
    anything fails after the commit (sending the response, a crash), an identical retry replays the
    original result; if anything fails before it, nothing was written and the retry runs again.
    """
    if key is None:
        with tenant_session(tenant_id) as conn:
            status, body = handler(conn)
        return JSONResponse(body, status_code=status)
    validate_key(key)
    replay = _claim(tenant_id, key, fingerprint)
    if replay is not None:
        return replay
    try:
        with tenant_session(tenant_id) as conn:
            status, body = handler(conn)
            conn.execute(
                update(idempotency_keys)
                .where(idempotency_keys.c.tenant_id == tenant_id, idempotency_keys.c.key == key)
                .values(status="COMPLETED", response_status=status, response_body=body)
            )
    except BaseException:
        _release(tenant_id, key)
        raise
    return JSONResponse(body, status_code=status)
