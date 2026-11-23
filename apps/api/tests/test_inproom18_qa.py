"""INPROOM-18 QA regressions: assertions express the required behavior, currently failing.

Run with the local stack: make test-api (or pytest this file with INPROOM_TEST_DATABASE_URL set).
"""

import threading
import uuid
from concurrent.futures import ThreadPoolExecutor

import pytest
from sqlalchemy import event

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


@pytest.mark.xfail(strict=True, reason="INPROOM-91 (QA-02) open: replay after post-commit failure")
def test_create_replays_after_idempotency_completion_store_failure(api, owner_engine, monkeypatch):
    workspace = api.workspace()
    headers = api.h(**{"Idempotency-Key": "qa-completion-failure"})
    path = f"/v1/workspaces/{workspace['id']}/projects"
    body = {"key": "RETRY", "name": "Retry after commit"}
    real_session = idempotency.tenant_session
    calls = 0

    def fail_completion(*args, **kwargs):
        nonlocal calls
        calls += 1
        if calls == 2:  # claim succeeded; project transaction committed; response storage fails
            raise RuntimeError("simulated database outage storing idempotency response")
        return real_session(*args, **kwargs)

    with monkeypatch.context() as patch:
        patch.setattr(idempotency, "tenant_session", fail_completion)
        with pytest.raises(RuntimeError, match="simulated database outage"):
            api.client.post(path, json=body, headers=headers)
    assert q(owner_engine, "SELECT count(*) FROM projects WHERE tenant_id=:t", t=api.tenant) == 1
    replay = api.client.post(path, json=body, headers=headers)
    assert replay.status_code == 201, replay.text
    assert replay.headers["Idempotent-Replayed"] == "true"


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
