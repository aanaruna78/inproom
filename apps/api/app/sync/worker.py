"""Sync worker and scheduler (INPROOM-141 / story INPROOM-25).

`run_once()` is one unit of work: recover expired leases, enqueue due sources (the scheduler tick), claim one
job, and run its handler in the job's tenant. `python -m app.worker` loops over it. Several workers can run at
once: claims use FOR UPDATE SKIP LOCKED under a lease. Each job logs one structured line (never secrets).
"""

import json
import logging
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy import text

from app.config import get_settings
from app.connectors.base import CredentialInvalid, NotFoundOrNoAccess, ProviderError, RateLimited
from app.db import get_engine, tenant_session
from app.db_schema import APP_ROLE
from app.sync import queue

log = logging.getLogger("inproom.sync")


class SkipJob(Exception):  # noqa: N818 - a control-flow signal, not an error
    """The source no longer needs syncing (deselected, paused, archived, kind disallowed)."""


class PermanentFailure(Exception):  # noqa: N818
    """Retrying cannot help (e.g. the space key now points at another space)."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


@dataclass
class JobContext:
    job: dict[str, Any]
    transports: dict[str, Any] = field(default_factory=dict)

    @property
    def tenant(self) -> uuid.UUID:
        return self.job["tenant_id"]

    @property
    def principal(self):
        return queue.system_principal(self.tenant)

    @property
    def trace_id(self) -> str:
        return f"sync-job:{self.job['id']}"


Handler = Callable[[JobContext], dict[str, Any]]
HANDLERS: dict[str, Handler] = {}


def handler(kind: str) -> Callable[[Handler], Handler]:
    def register(fn: Handler) -> Handler:
        HANDLERS[kind] = fn
        return fn

    return register


def _maintenance(sql: str, **params: Any) -> Any:
    """Call one of the queue's cross-tenant functions as the app role (no tenant bound)."""
    with get_engine().begin() as conn:
        conn.execute(text(f"SET LOCAL ROLE {APP_ROLE}"))
        return conn.execute(text(sql), params).all()


def schedule_tick(tenant: uuid.UUID | None = None) -> dict[str, int]:
    settings = get_settings()
    recovered = _maintenance("SELECT requeue_expired_sync_jobs(:t)", t=tenant)[0][0]
    queued = _maintenance(
        "SELECT enqueue_due_syncs(:i, :f, :t)",
        i=settings.sync_interval_seconds,
        f=settings.sync_full_interval_seconds,
        t=tenant,
    )[0][0]
    return {"recovered": recovered, "queued": queued}


def claim(worker_id: str, tenant: uuid.UUID | None = None) -> tuple[uuid.UUID, uuid.UUID] | None:
    rows = _maintenance(
        "SELECT job_id, job_tenant FROM claim_sync_job(:w, :l, :t)",
        w=worker_id,
        l=get_settings().sync_lease_seconds,
        t=tenant,
    )
    return (rows[0].job_id, rows[0].job_tenant) if rows else None


def _error(code: str, exc: Exception) -> dict[str, str]:
    # Provider error messages never carry credentials (see app.connectors.http); keep them short anyway.
    return {"code": code, "message": str(exc)[:300] or type(exc).__name__}


def run_job(job_id: uuid.UUID, tenant: uuid.UUID, transports: dict[str, Any] | None = None) -> dict[str, Any]:
    """Run one claimed job and record its outcome. Returns a summary (also logged)."""
    with tenant_session(tenant) as conn:
        job = queue.load(conn, job_id)
    started = time.monotonic()
    ctx = JobContext(job, transports or {})
    outcome: dict[str, Any] = {"status": "SUCCEEDED"}
    try:
        outcome["result"] = HANDLERS[job["source_kind"]](ctx)
    except queue.LostLease:
        outcome = {
            "status": "LOST_LEASE"
        }  # everything the job wrote was rolled back; the new holder redoes it
    except SkipJob as exc:
        with tenant_session(tenant) as conn:
            queue.skip(conn, job, str(exc))
        outcome = {"status": "SKIPPED", "reason": str(exc)}
    except RateLimited as exc:
        error = _error("PROVIDER_RATE_LIMITED", exc)
        with tenant_session(tenant) as conn:
            status = queue.retry(conn, job, error, after_s=exc.retry_after, count_attempt=False)
        outcome = {"status": status, "error": error["code"], "retry_after": exc.retry_after}
    except (CredentialInvalid, NotFoundOrNoAccess) as exc:
        code = "CREDENTIAL_INVALID" if isinstance(exc, CredentialInvalid) else "SOURCE_NOT_ACCESSIBLE"
        with tenant_session(tenant) as conn:
            queue.fail(conn, job, _error(code, exc))
        outcome = {"status": "FAILED", "error": code}
    except PermanentFailure as exc:
        with tenant_session(tenant) as conn:
            queue.fail(conn, job, _error(exc.code, exc))
        outcome = {"status": "FAILED", "error": exc.code}
    except Exception as exc:  # noqa: BLE001 - transient provider trouble and unexpected bugs both retry
        code = getattr(exc, "code", None) if isinstance(exc, ProviderError) else "INTERNAL_ERROR"
        error = _error(code or "PROVIDER_ERROR", exc)
        with tenant_session(tenant) as conn:
            status = queue.retry(
                conn, job, error, after_s=queue.backoff_seconds(job["attempts"]), count_attempt=True
            )
        outcome = {"status": status, "error": error["code"]}
        if code == "INTERNAL_ERROR":
            log.exception("sync job %s failed unexpectedly", job["id"])
    summary = {
        "event": "sync.job",
        "job_id": str(job["id"]),
        "tenant_id": str(tenant),
        "source_kind": job["source_kind"],
        "source_id": str(job["source_id"]),
        "reason": job["reason"],
        "mode": job["mode"],
        "attempt": job["attempts"],
        "duration_ms": round((time.monotonic() - started) * 1000),
    } | outcome
    log.info(json.dumps(summary, default=str))
    return summary


def run_once(
    worker_id: str = "worker",
    transports: dict[str, Any] | None = None,
    schedule: bool = True,
    tenant: uuid.UUID | None = None,
) -> dict[str, Any] | None:
    """Recover leases + enqueue due work (if `schedule`), then claim and run one job. None = nothing to do."""
    if schedule:
        schedule_tick(tenant)
    claimed = claim(worker_id, tenant)
    if claimed is None:
        return None
    return run_job(*claimed, transports=transports)


# Register the per-kind handlers.
from app.sync import handlers as _handlers  # noqa: E402, F401
