"""AC6-7: idempotency, permission guard and audit writer, exercised through a test-only route that uses
the same building blocks the workspace/project endpoints (INPROOM-84) will use."""

import threading
import uuid
from typing import Annotated

import pytest
from fastapi import Body, Header, Request
from sqlalchemy import insert, text

from app.audit import record
from app.auth import CurrentPrincipal
from app.authz import NotFound, require
from app.db import tenant_session
from app.db_schema import outbox_events
from app.idempotency import IdempotencyError, request_fingerprint, run_idempotent
from app.main import create_app
from tests.auth_helpers import bearer

pytestmark = pytest.mark.integration


def build_probe_app(fail_after_write: dict[str, bool]):
    app = create_app()

    @app.post("/test/probe", status_code=201)
    def probe(
        request: Request,
        principal: CurrentPrincipal,
        body: Annotated[dict, Body()],
        idempotency_key: Annotated[str | None, Header(alias="Idempotency-Key")] = None,
    ):
        require(principal, "probe.create", request, target_type="probe")

        def handler(conn):
            event_id = conn.execute(
                insert(outbox_events)
                .values(tenant_id=principal.tenant_id, type="probe.created", payload=body)
                .returning(outbox_events.c.id)
            ).scalar_one()
            record(
                conn,
                principal,
                "probe.create",
                "probe",
                str(event_id),
                trace_id=request.state.trace_id,
                payload=body,
            )
            if fail_after_write.get("fail"):
                raise RuntimeError("forced failure after write")
            return 201, {"id": str(event_id), "name": body.get("name")}

        fp = request_fingerprint("POST", "/test/probe", body)
        return run_idempotent(principal.tenant_id, idempotency_key, fp, handler)

    @app.get("/test/probe/{probe_id}")
    def get_probe(probe_id: uuid.UUID, principal: CurrentPrincipal):
        with tenant_session(principal.tenant_id) as conn:
            found = conn.execute(text("SELECT id FROM outbox_events WHERE id = :i"), {"i": probe_id}).first()
        if not found:
            raise NotFound("Probe")
        return {"id": str(probe_id)}

    return app


@pytest.fixture
def failure_switch():
    return {"fail": False}


@pytest.fixture
def probe(client_for, test_database_url, failure_switch):
    return client_for(test_database_url, build_probe_app(failure_switch))


def _count(owner_engine, sql, **params):
    with owner_engine.connect() as conn:
        return conn.execute(text(sql), params).scalar()


def test_same_key_and_body_writes_once_and_replays(probe, tokens, make_tenant, owner_engine):
    tid = make_tenant()
    h = bearer(tokens.mint(tenant_id=tid, roles=["tenant_admin"])) | {"Idempotency-Key": "create-1"}
    first = probe.post("/test/probe", json={"name": "one"}, headers=h)
    second = probe.post("/test/probe", json={"name": "one"}, headers=h)
    assert first.status_code == second.status_code == 201
    assert second.json() == first.json()
    assert second.headers["Idempotent-Replayed"] == "true"
    assert "Idempotent-Replayed" not in first.headers
    assert _count(owner_engine, "SELECT count(*) FROM outbox_events WHERE tenant_id=:t", t=tid) == 1
    assert (
        _count(
            owner_engine, "SELECT count(*) FROM audit_events WHERE tenant_id=:t AND outcome='SUCCESS'", t=tid
        )
        == 1
    )


def test_same_key_different_body_is_rejected_without_write(probe, tokens, make_tenant, owner_engine):
    tid = make_tenant()
    h = bearer(tokens.mint(tenant_id=tid, roles=["tenant_admin"])) | {"Idempotency-Key": "create-2"}
    assert probe.post("/test/probe", json={"name": "one"}, headers=h).status_code == 201
    r = probe.post("/test/probe", json={"name": "two"}, headers=h)
    assert r.status_code == 422
    assert r.json()["code"] == "IDEMPOTENCY_KEY_REUSED"
    assert _count(owner_engine, "SELECT count(*) FROM outbox_events WHERE tenant_id=:t", t=tid) == 1


def test_same_key_in_another_tenant_is_independent(probe, tokens, make_tenant):
    a, b = make_tenant(), make_tenant()
    for tid in (a, b):
        h = bearer(tokens.mint(tenant_id=tid, roles=["tenant_admin"])) | {"Idempotency-Key": "shared-key"}
        r = probe.post("/test/probe", json={"name": "x"}, headers=h)
        assert r.status_code == 201 and "Idempotent-Replayed" not in r.headers


def test_concurrent_duplicate_gets_409(make_tenant, test_database_url, monkeypatch):
    monkeypatch.setenv("INPROOM_DATABASE_URL", test_database_url)
    tid = make_tenant()
    entered, release = threading.Event(), threading.Event()
    results: dict[str, object] = {}

    def slow_handler(conn):
        entered.set()
        release.wait(5)
        return 201, {"ok": True}

    def first():
        results["first"] = run_idempotent(tid, "race", "fp", slow_handler).status_code

    t = threading.Thread(target=first)
    t.start()
    assert entered.wait(5)
    with pytest.raises(IdempotencyError) as exc:
        run_idempotent(tid, "race", "fp", lambda conn: (201, {"ok": True}))
    release.set()
    t.join(5)
    assert exc.value.status == 409 and exc.value.code == "IDEMPOTENCY_IN_PROGRESS"
    assert results["first"] == 201


def test_failed_handler_rolls_back_and_releases_key(probe, tokens, make_tenant, owner_engine, failure_switch):
    tid = make_tenant()
    h = bearer(tokens.mint(tenant_id=tid, roles=["tenant_admin"])) | {"Idempotency-Key": "retry-me"}
    failure_switch["fail"] = True
    with pytest.raises(RuntimeError):
        probe.post("/test/probe", json={"name": "x"}, headers=h)
    assert _count(owner_engine, "SELECT count(*) FROM outbox_events WHERE tenant_id=:t", t=tid) == 0
    assert _count(owner_engine, "SELECT count(*) FROM audit_events WHERE tenant_id=:t", t=tid) == 0
    failure_switch["fail"] = False
    retry = probe.post("/test/probe", json={"name": "x"}, headers=h)
    assert retry.status_code == 201 and "Idempotent-Replayed" not in retry.headers


def test_invalid_idempotency_key_rejected(probe, tokens, make_tenant):
    h = bearer(tokens.mint(tenant_id=make_tenant(), roles=["tenant_admin"])) | {"Idempotency-Key": "x" * 129}
    r = probe.post("/test/probe", json={"name": "x"}, headers=h)
    assert r.status_code == 400 and r.json()["code"] == "IDEMPOTENCY_KEY_INVALID"


def test_missing_permission_is_403_and_denied_audit_persists(probe, tokens, make_tenant, owner_engine):
    tid = make_tenant()
    r = probe.post(
        "/test/probe",
        json={"name": "x"},
        headers=bearer(tokens.mint(tenant_id=tid, user_id="dave", roles=["developer"])),
    )
    assert r.status_code == 403
    assert r.headers["content-type"] == "application/problem+json"
    assert r.json()["code"] == "FORBIDDEN"
    with owner_engine.connect() as conn:
        row = conn.execute(
            text("SELECT actor_id, action, outcome, reason, trace_id FROM audit_events WHERE tenant_id=:t"),
            {"t": tid},
        ).one()
    assert row.actor_id == "dave" and row.action == "probe.create" and row.outcome == "DENIED"
    assert row.reason == "missing permission probe.create"
    assert row.trace_id == r.headers["X-Trace-Id"]
    assert _count(owner_engine, "SELECT count(*) FROM outbox_events WHERE tenant_id=:t", t=tid) == 0


def test_success_audit_row_has_actor_target_trace_and_hash_only(probe, tokens, make_tenant, owner_engine):
    tid = make_tenant()
    r = probe.post(
        "/test/probe",
        json={"name": "secret-ish value"},
        headers=bearer(tokens.mint(tenant_id=tid, user_id="alice", roles=["tenant_admin"])),
    )
    assert r.status_code == 201
    with owner_engine.connect() as conn:
        row = conn.execute(text("SELECT * FROM audit_events WHERE tenant_id=:t"), {"t": tid}).mappings().one()
    assert row["actor_id"] == "alice" and row["outcome"] == "SUCCESS"
    assert row["target_type"] == "probe" and row["target_id"] == r.json()["id"]
    assert row["trace_id"] == r.headers["X-Trace-Id"]
    assert row["payload_sha256"].startswith("sha256:") and len(row["payload_sha256"]) == 71
    assert "secret-ish value" not in str(dict(row))


def test_other_tenant_resource_is_indistinguishable_from_missing(probe, tokens, make_tenant):
    a, b = make_tenant(), make_tenant()
    created = probe.post(
        "/test/probe", json={"name": "a"}, headers=bearer(tokens.mint(tenant_id=a, roles=["tenant_admin"]))
    ).json()["id"]
    b_headers = bearer(tokens.mint(tenant_id=b, roles=["tenant_admin"]))
    other_tenant = probe.get(f"/test/probe/{created}", headers=b_headers)
    missing = probe.get(f"/test/probe/{uuid.uuid4()}", headers=b_headers)
    assert other_tenant.status_code == missing.status_code == 404
    strip = lambda body: {k: v for k, v in body.items() if k != "trace_id"}  # noqa: E731
    assert strip(other_tenant.json()) == strip(missing.json())
