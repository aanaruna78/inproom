"""INPROOM-18 QA regressions: assertions express the required behavior, currently failing.

Run with the local stack: make test-api (or pytest this file with INPROOM_TEST_DATABASE_URL set).
"""

import threading
import uuid
from concurrent.futures import ThreadPoolExecutor

import pytest
from sqlalchemy import event, text

from app import idempotency
from app.config import get_settings
from app.db import get_engine
from tests import test_workspaces_projects_integration as helpers

api = helpers.api
q = helpers.q

pytestmark = pytest.mark.integration


@pytest.mark.parametrize("method", ["GET", "PATCH"])
def test_cross_tenant_project_probe_is_audited(api, owner_engine, method):
    project = api.project(api.workspace()["id"])
    attacker = uuid.uuid4()
    api.workspace(tenant=attacker)
    headers = api.h(tenant=attacker, **{"If-Match": '"1"'})
    kwargs = {"json": {"name": "Unauthorized change"}} if method == "PATCH" else {}
    response = api.client.request(method, f"/v1/projects/{project['id']}", headers=headers, **kwargs)
    assert response.status_code == 404
    assert (
        q(
            owner_engine,
            "SELECT count(*) FROM audit_events WHERE tenant_id=:t AND target_id=:p AND outcome='DENIED'",
            t=attacker,
            p=project["id"],
        )
        == 1
    )


def test_create_replays_after_idempotency_completion_store_failure(api, owner_engine, monkeypatch):
    """QA-02 / INPROOM-91 guard: anything failing AFTER the commit (building/sending the response, a crash)
    must still let an identical retry replay the original 201 without new writes.

    Note for QA: this case also passes on the pre-fix code, so it is a guard, not the defect regression.
    The defect regression is test_failure_before_commit_leaves_nothing_and_retry_runs_fresh, which uses
    QA's original injection point. With the fix the project and its stored response commit in ONE
    transaction, so the state QA reproduced ("project committed, response not stored") can no longer
    occur.
    """
    workspace = api.workspace()
    headers = api.h(**{"Idempotency-Key": "qa-completion-failure"})
    path = f"/v1/workspaces/{workspace['id']}/projects"
    body = {"key": "RETRY", "name": "Retry after commit"}
    real_response = idempotency.JSONResponse

    def fail_after_commit(*args, **kwargs):
        raise RuntimeError("simulated database outage storing idempotency response")

    with monkeypatch.context() as patch:
        patch.setattr(idempotency, "JSONResponse", fail_after_commit)
        with pytest.raises(RuntimeError, match="simulated database outage"):
            api.client.post(path, json=body, headers=headers)
    assert idempotency.JSONResponse is real_response
    assert q(owner_engine, "SELECT count(*) FROM projects WHERE tenant_id=:t", t=api.tenant) == 1
    replay = api.client.post(path, json=body, headers=headers)
    assert replay.status_code == 201, replay.text
    assert replay.headers["Idempotent-Replayed"] == "true"
    # The replay wrote nothing new.
    for table in ("projects", "project_members", "project_settings_versions"):
        assert q(owner_engine, f"SELECT count(*) FROM {table} WHERE tenant_id=:t", t=api.tenant) == 1, table
    assert (
        q(
            owner_engine,
            "SELECT count(*) FROM audit_events WHERE tenant_id=:t AND action='project.create'",
            t=api.tenant,
        )
        == 1
    )
    assert (
        q(
            owner_engine,
            "SELECT count(*) FROM outbox_events WHERE tenant_id=:t AND type='project.created'",
            t=api.tenant,
        )
        == 1
    )


def test_failure_before_commit_leaves_nothing_and_retry_runs_fresh(api, owner_engine, monkeypatch):
    """QA-02 / INPROOM-91 DEFECT REGRESSION - QA's original fault injection (second
    idempotency.tenant_session call). Pre-fix: the project committed (1 row) while the key stayed
    IN_PROGRESS, so the retry got 409. Post-fix that call is the single business+response transaction:
    the failure persists nothing, frees the key, and the identical retry succeeds exactly once.
    """
    workspace = api.workspace()
    headers = api.h(**{"Idempotency-Key": "qa-pre-commit-failure"})
    path = f"/v1/workspaces/{workspace['id']}/projects"
    body = {"key": "PRECOMMIT", "name": "Fails before commit"}
    real_session = idempotency.tenant_session
    calls = 0

    def fail_second_session(*args, **kwargs):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise RuntimeError("simulated outage opening the business transaction")
        return real_session(*args, **kwargs)

    with monkeypatch.context() as patch:
        patch.setattr(idempotency, "tenant_session", fail_second_session)
        with pytest.raises(RuntimeError, match="simulated outage"):
            api.client.post(path, json=body, headers=headers)
    assert q(owner_engine, "SELECT count(*) FROM projects WHERE tenant_id=:t", t=api.tenant) == 0
    assert q(owner_engine, "SELECT count(*) FROM idempotency_keys WHERE tenant_id=:t", t=api.tenant) == 0
    retry = api.client.post(path, json=body, headers=headers)
    assert retry.status_code == 201 and "Idempotent-Replayed" not in retry.headers
    assert q(owner_engine, "SELECT count(*) FROM projects WHERE tenant_id=:t", t=api.tenant) == 1


def test_abandoned_claim_is_reclaimed_after_lease(api, owner_engine):
    """A claim left IN_PROGRESS by a crashed process (nothing committed) must not block retries forever."""
    workspace = api.workspace()
    path = f"/v1/workspaces/{workspace['id']}/projects"
    body = {"key": "LEASE", "name": "Lease expiry"}
    fingerprint = idempotency.request_fingerprint(
        "POST", path, body | {"description": None, "mode": "NEW", "process_package": "sdlc@1"}
    )
    with owner_engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO idempotency_keys (tenant_id, key, request_sha256, status, created_at) "
                "VALUES (:t, 'crashed', :f, 'IN_PROGRESS', now() - interval '10 minutes')"
            ),
            {"t": api.tenant, "f": fingerprint},
        )
    r = api.client.post(path, json=body, headers=api.h(**{"Idempotency-Key": "crashed"}))
    assert r.status_code == 201, r.text
    assert "Idempotent-Replayed" not in r.headers


def test_fresh_in_progress_claim_still_conflicts(api, owner_engine):
    """Within the lease, a genuinely in-flight duplicate is still refused (no double execution)."""
    workspace = api.workspace()
    path = f"/v1/workspaces/{workspace['id']}/projects"
    body = {"key": "INFLIGHT", "name": "In flight"}
    fingerprint = idempotency.request_fingerprint(
        "POST", path, body | {"description": None, "mode": "NEW", "process_package": "sdlc@1"}
    )
    with owner_engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO idempotency_keys (tenant_id, key, request_sha256, status) "
                "VALUES (:t, 'inflight', :f, 'IN_PROGRESS')"
            ),
            {"t": api.tenant, "f": fingerprint},
        )
    r = api.client.post(path, json=body, headers=api.h(**{"Idempotency-Key": "inflight"}))
    assert r.status_code == 409 and r.json()["code"] == "IDEMPOTENCY_IN_PROGRESS"


@pytest.mark.xfail(strict=True, reason="INPROOM-92 (QA-03) open: concurrent creates exceed limit")
def test_concurrent_project_creates_respect_workspace_limit(api, owner_engine, monkeypatch):
    monkeypatch.setenv("INPROOM_WORKSPACE_PROJECT_LIMIT", "1")
    get_settings.cache_clear()
    workspace = api.workspace()
    headers = api.h()
    barrier = threading.Barrier(2)
    engine = get_engine()

    def synchronize_limit_reads(conn, cursor, statement, parameters, context, executemany):
        if statement.startswith("SELECT count(*)") and "FROM projects" in statement:
            barrier.wait(timeout=10)

    def create(key):
        return api.client.post(
            f"/v1/workspaces/{workspace['id']}/projects",
            json={"key": key, "name": f"Concurrent {key}"},
            headers=headers,
        )

    event.listen(engine, "after_cursor_execute", synchronize_limit_reads)
    try:
        with ThreadPoolExecutor(max_workers=2) as pool:
            responses = list(pool.map(create, ["QAONE", "QATWO"]))
    finally:
        event.remove(engine, "after_cursor_execute", synchronize_limit_reads)
    assert sorted(r.status_code for r in responses) == [201, 422]
    assert q(owner_engine, "SELECT count(*) FROM projects WHERE tenant_id=:t", t=api.tenant) == 1
