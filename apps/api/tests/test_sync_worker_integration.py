"""INPROOM-141 (story INPROOM-25 AC 4, 5, 7): durable sync queue, worker, scheduler and retry policy.

Driven through `run_once(tenant=...)` against the fake Confluence; the tenant filter keeps each test to its
own jobs in the shared test database.
"""

import uuid

import pytest
from sqlalchemy import text

from app.db import get_engine, tenant_session
from app.sync import queue, worker
from tests import test_confluence_sources_integration as helpers
from tests.fakes import confluence as fc
from tests.sync_helpers import run_sync

env = helpers.env
selected = helpers.selected
pytestmark = [pytest.mark.integration, pytest.mark.usefixtures("no_content_jobs")]


def run(e, **kw):
    return run_sync({"confluence": e.conf.transport()}, e.tenant, worker_id="w1", **kw)


def job(e):
    return e.q("SELECT status FROM sync_jobs WHERE tenant_id=:t ORDER BY created_at DESC LIMIT 1")


def _engine():
    """The app engine connects as the database owner in tests; used only for test clock control."""
    return get_engine()


def make_due(e):
    with _engine().begin() as conn:
        conn.execute(
            text("UPDATE source_sync_state SET next_due_at = now() WHERE tenant_id=:t"), {"t": e.tenant}
        )
        conn.execute(
            text("UPDATE sync_jobs SET run_after = now() WHERE tenant_id=:t AND status='QUEUED'"),
            {"t": e.tenant},
        )


def enqueue(e, reason="manual", mode="incremental"):
    with tenant_session(e.tenant) as conn:
        return queue.enqueue(conn, e.tenant, uuid.UUID(e.pid), "confluence", uuid.UUID(e.sid), reason, mode)


def test_schedule_runs_due_sources_and_records_state(selected):
    e = selected
    first = run(e)
    assert first["status"] == "SUCCEEDED" and first["reason"] == "schedule" and first["mode"] == "full"
    assert run(e) is None  # nothing due until the interval passes
    state = e.q("SELECT row_to_json(s)::text FROM source_sync_state s WHERE tenant_id=:t")
    assert '"consecutive_failures":0' in state and '"last_full_at":"' in state
    e.conf.edit(101, "<p>scheduled pickup</p>")
    make_due(e)
    second = run(e)
    assert second["status"] == "SUCCEEDED" and second["mode"] == "incremental"
    assert second["result"]["counts"]["updated"] == 1
    assert e.events("source.confluence.page.changed") == 4  # 3 from selection + this one, exactly once


def test_enqueues_coalesce_per_source(selected):
    e = selected
    for _ in range(10):
        first = enqueue(e)
    assert first["triggers"] == 10 and e.q("SELECT count(*) FROM sync_jobs WHERE tenant_id=:t") == 1
    assert enqueue(e, mode="full")["mode"] == "full"  # a full request upgrades the queued job
    with _engine().begin() as conn:  # simulate it running
        conn.execute(
            text("UPDATE sync_jobs SET status='RUNNING', locked_by='x' WHERE tenant_id=:t"), {"t": e.tenant}
        )
    enqueue(e)
    enqueue(e)
    assert e.q("SELECT count(*) FROM sync_jobs WHERE tenant_id=:t") == 2  # one running + one waiting


def test_rate_limit_requeues_at_retry_after_without_using_an_attempt(selected):
    e = selected
    enqueue(e)
    e.conf.failures = ["ratelimit"]
    out = run(e, schedule=False)
    assert out["status"] == "QUEUED" and out["retry_after"] == 42
    row = e.q(
        "SELECT json_build_object('a', attempts, 'wait', extract(epoch FROM run_after - now()))::text "
        "FROM sync_jobs WHERE tenant_id=:t"
    )
    assert '"a" : 0' in row and 35 < float(row.split('"wait" : ')[1].rstrip("}")) <= 42
    assert run(e, schedule=False) is None  # not runnable before Retry-After


def test_outage_backs_off_then_dead_then_recovers(selected):
    e = selected
    enqueue(e)
    for attempt in range(1, queue.MAX_ATTEMPTS + 1):
        e.conf.failures = ["500"] * 3
        make_due(e)
        out = run(e, schedule=False)
        assert out["status"] == ("DEAD" if attempt == queue.MAX_ATTEMPTS else "QUEUED"), (attempt, out)
    assert e.q("SELECT consecutive_failures FROM source_sync_state WHERE tenant_id=:t") == queue.MAX_ATTEMPTS
    assert e.q("SELECT count(*) FROM audit_events WHERE tenant_id=:t AND action='source.sync.dead'") == 1
    assert (
        queue.backoff_seconds(1) == 30
        and queue.backoff_seconds(3) == 120
        and queue.backoff_seconds(20) == 1800
    )
    e.conf.failures = []
    enqueue(e)
    assert run(e, schedule=False)["status"] == "SUCCEEDED"  # the provider is back: the next run catches up
    assert e.q("SELECT consecutive_failures FROM source_sync_state WHERE tenant_id=:t") == 0


def test_invalid_credential_fails_without_retry(selected):
    e = selected
    enqueue(e)
    e.conf.users[fc.VALID_TOKEN].valid = False
    out = run(e, schedule=False)
    assert out["status"] == "FAILED" and out["error"] == "CREDENTIAL_INVALID"
    assert e.q("SELECT count(*) FROM sync_jobs WHERE tenant_id=:t AND status='QUEUED'") == 0


def test_crashed_worker_lease_is_recovered_and_effects_commit_once(selected):
    e = selected
    enqueue(e)
    crashed = worker.claim("crashed-worker", e.tenant)  # claims, then "dies" before finishing
    assert crashed is not None
    with _engine().begin() as conn:
        conn.execute(
            text("UPDATE sync_jobs SET locked_until = now() - interval '1 second' WHERE tenant_id=:t"),
            {"t": e.tenant},
        )
    e.conf.edit(101, "<p>after crash</p>")
    out = run(e)  # schedule tick recovers the expired lease, then this worker re-runs the job
    assert out["status"] == "SUCCEEDED" and out["reason"] == "recovery"
    assert e.events("source.confluence.page.changed") == 4
    # The crashed worker waking up cannot commit anything: its lease is gone.
    with tenant_session(e.tenant) as conn:
        stale = queue.load(conn, crashed[0]) | {"locked_by": "crashed-worker"}
    with pytest.raises(queue.LostLease), tenant_session(e.tenant) as conn:
        queue.succeed(conn, stale, {"late": True})


def test_jobs_and_state_are_tenant_scoped(selected, make_tenant):
    e = selected
    run(e)
    other = make_tenant()
    with tenant_session(other) as conn:
        assert conn.execute(text("SELECT count(*) FROM sync_jobs")).scalar() == 0
        assert conn.execute(text("SELECT count(*) FROM source_sync_state")).scalar() == 0
    assert worker.claim("w2", other) is None  # the tenant filter never hands out someone else's job
    assert e.q("SELECT count(*) FROM sync_jobs WHERE tenant_id=:t AND status='SUCCEEDED'") == 1


def test_confluence_edit_and_deletion_recovered_once(selected):
    """INPROOM-142: Confluence edits and deletions with no webhook are recovered once by the scheduler."""
    e = selected
    run(e)
    e.conf.edit(101, "<p>edited, no webhook</p>")
    e.conf.pages[102].status = "trashed"
    make_due(e)
    counts = run(e)["result"]["counts"]
    assert (counts["updated"], counts["deleted"]) == (1, 1)
    make_due(e)
    assert run(e)["result"]["changed"] is False
    assert e.events("source.confluence.page.changed") == 4 and e.events("source.confluence.page.deleted") == 1
