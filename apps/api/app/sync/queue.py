"""Durable sync job queue (INPROOM-141 / story INPROOM-25).

Jobs live in PostgreSQL (`sync_jobs`, tenant-scoped with RLS). Enqueueing is coalesced: a partial unique index
allows one QUEUED job per source, and further triggers attach to it (`triggers` count, mode upgraded to full).
A worker claims a job with a lease; every write a job makes - its effects, its sync state and its own
completion - is committed in one transaction that first re-checks the lease (`LostLease` otherwise), so a job
whose worker crashed and was re-run can never commit twice.

Retry policy:
- RateLimited (429)          -> back to QUEUED at Retry-After; the attempt is not counted;
- transient (5xx, timeout)   -> back to QUEUED after 30 s x 2^(n-1), max 30 min; DEAD after MAX_ATTEMPTS;
- credential invalid / gone  -> FAILED, no retry (the connection's health check reports why).
"""

import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import Connection, case, select, text, update
from sqlalchemy.dialects.postgresql import insert as pg_insert

from app import audit
from app.auth import Principal
from app.config import get_settings
from app.db_schema import source_sync_state, sync_jobs

SYSTEM_ACTOR = "system:sync-worker"
MAX_ATTEMPTS = 6
BASE_BACKOFF_S = 30
MAX_BACKOFF_S = 1800
KINDS = ("git", "jira", "confluence", "connection")


class LostLease(Exception):
    """The job is no longer ours (lease expired and it was re-queued or re-claimed): roll everything back."""


def system_principal(tenant_id: uuid.UUID) -> Principal:
    return Principal(user_id=SYSTEM_ACTOR, tenant_id=tenant_id)


def backoff_seconds(attempts: int) -> int:
    return min(BASE_BACKOFF_S * 2 ** max(attempts - 1, 0), MAX_BACKOFF_S)


def enqueue(
    conn: Connection,
    tenant_id: uuid.UUID,
    project_id: uuid.UUID,
    kind: str,
    source_id: uuid.UUID,
    reason: str,
    mode: str = "incremental",
    hints: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Queue a sync for one source, or join the job already queued for it. Returns the job row."""
    stmt = pg_insert(sync_jobs).values(
        tenant_id=tenant_id,
        project_id=project_id,
        source_kind=kind,
        source_id=source_id,
        reason=reason,
        mode=mode,
        hints=hints or {},
    )
    stmt = stmt.on_conflict_do_update(
        index_elements=[sync_jobs.c.source_kind, sync_jobs.c.source_id],
        index_where=text("status = 'QUEUED'"),
        set_={
            "triggers": sync_jobs.c.triggers + 1,
            "mode": case((stmt.excluded.mode == "full", "full"), else_=sync_jobs.c.mode),
            "hints": sync_jobs.c.hints.op("||")(stmt.excluded.hints),
        },
    )
    return dict(conn.execute(stmt.returning(*sync_jobs.c)).mappings().one())


def load(conn: Connection, job_id: uuid.UUID) -> dict[str, Any]:
    return dict(conn.execute(select(sync_jobs).where(sync_jobs.c.id == job_id)).mappings().one())


def _own(conn: Connection, job: dict[str, Any], **values: Any) -> None:
    """Update our job only while we still hold its lease; otherwise abort the whole transaction."""
    done = conn.execute(
        update(sync_jobs)
        .where(
            sync_jobs.c.id == job["id"],
            sync_jobs.c.status == "RUNNING",
            sync_jobs.c.locked_by == job["locked_by"],
        )
        .values(**values)
    ).rowcount
    if done != 1:
        raise LostLease(f"sync job {job['id']} is no longer held by {job['locked_by']}")


def _state(conn: Connection, job: dict[str, Any], **values: Any) -> None:
    now = datetime.now(UTC)
    stmt = pg_insert(source_sync_state).values(
        tenant_id=job["tenant_id"],
        project_id=job["project_id"],
        source_kind=job["source_kind"],
        source_id=job["source_id"],
        last_attempt_at=now,
        updated_at=now,
        **values,
    )
    conn.execute(
        stmt.on_conflict_do_update(
            index_elements=[source_sync_state.c.source_kind, source_sync_state.c.source_id],
            set_={"last_attempt_at": now, "updated_at": now} | {k: stmt.excluded[k] for k in values},
        )
    )


def watermark(conn: Connection, job: dict[str, Any]) -> dict[str, Any]:
    row = conn.execute(
        select(source_sync_state.c.watermark).where(
            source_sync_state.c.source_kind == job["source_kind"],
            source_sync_state.c.source_id == job["source_id"],
        )
    ).first()
    return dict(row.watermark) if row else {}


def succeed(
    conn: Connection,
    job: dict[str, Any],
    result: dict[str, Any],
    new_watermark: dict[str, Any] | None = None,
) -> None:
    """Complete the job in the caller's transaction, together with the effects it already wrote."""
    now = datetime.now(UTC)
    _own(conn, job, status="SUCCEEDED", result=result, finished_at=now, locked_until=None, last_error=None)
    values: dict[str, Any] = {
        "last_success_at": now,
        "consecutive_failures": 0,
        "last_error": None,
        "next_due_at": now + timedelta(seconds=_interval(job)),
    }
    if new_watermark is not None:
        values["watermark"] = new_watermark
    if job["mode"] == "full":
        values["last_full_at"] = now
    _state(conn, job, **values)


def _interval(job: dict[str, Any]) -> int:
    settings = get_settings()
    if job["source_kind"] == "connection":
        return settings.health_interval_seconds
    if job["source_kind"] == "purge":
        return settings.content_purge_interval_seconds
    return settings.sync_interval_seconds


def skip(conn: Connection, job: dict[str, Any], reason: str) -> None:
    _own(
        conn,
        job,
        status="SKIPPED",
        finished_at=datetime.now(UTC),
        locked_until=None,
        result={"skipped": reason},
    )


def _failures(conn: Connection, job: dict[str, Any]) -> int:
    row = conn.execute(
        select(source_sync_state.c.consecutive_failures).where(
            source_sync_state.c.source_kind == job["source_kind"],
            source_sync_state.c.source_id == job["source_id"],
        )
    ).first()
    return (row.consecutive_failures if row else 0) + 1


def retry(
    conn: Connection, job: dict[str, Any], error: dict[str, str], after_s: int, count_attempt: bool
) -> str:
    """Back to QUEUED (or DEAD when attempts are exhausted). Returns the new status."""
    attempts = job["attempts"] if count_attempt else max(job["attempts"] - 1, 0)
    if count_attempt and attempts >= MAX_ATTEMPTS:
        dead(conn, job, error)
        return "DEAD"
    _own(
        conn,
        job,
        status="QUEUED",
        attempts=attempts,
        locked_by=None,
        locked_until=None,
        last_error=error,
        run_after=datetime.now(UTC) + timedelta(seconds=after_s),
    )
    _state(conn, job, consecutive_failures=_failures(conn, job), last_error=error)
    return "QUEUED"


def fail(conn: Connection, job: dict[str, Any], error: dict[str, str]) -> None:
    _own(conn, job, status="FAILED", finished_at=datetime.now(UTC), locked_until=None, last_error=error)
    _state(conn, job, consecutive_failures=_failures(conn, job), last_error=error)


def dead(conn: Connection, job: dict[str, Any], error: dict[str, str]) -> None:
    _own(conn, job, status="DEAD", finished_at=datetime.now(UTC), locked_until=None, last_error=error)
    _state(conn, job, consecutive_failures=_failures(conn, job), last_error=error)
    audit.record(
        conn,
        system_principal(job["tenant_id"]),
        "source.sync.dead",
        "sync_job",
        str(job["id"]),
        outcome="FAILED",
        payload={
            "source_kind": job["source_kind"],
            "source_id": str(job["source_id"]),
            "attempts": job["attempts"],
            "error": error.get("code"),
        },
        project_id=job["project_id"],
        reason=f"sync gave up after {job['attempts']} attempts: {error.get('code')}",
    )
