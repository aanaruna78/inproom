"""INPROOM-145 (story INPROOM-26 AC 1, 2, 3, 6): automatic health, immediate invalidation, exclusion,
freshness in the sync scope, and the plain-language error catalog."""

import re
import uuid
from pathlib import Path

import pytest
from sqlalchemy import text

from app.db import get_engine, tenant_session
from app.sync import queue, worker
from app.sync.errors import CATALOG
from tests import test_confluence_sources_integration as helpers
from tests.fakes import confluence as fc
from tests.sync_helpers import due

env = helpers.env
selected = helpers.selected
pytestmark = pytest.mark.integration


def drain(e, schedule=True):
    """Run every runnable job for this tenant (schedule tick first)."""
    done = []
    while out := worker.run_once(
        "w", transports={"confluence": e.conf.transport()}, schedule=schedule, tenant=e.tenant
    ):
        done.append(out)
        schedule = False
    return done


def scope_item(e):
    scope = e.client.get(f"/v1/projects/{e.pid}/sources/scope", headers=e.h()).json()
    return (scope["confluence_spaces"] + [x for x in scope["excluded"] if x["kind"] == "confluence"])[0]


def connection(e):
    return e.client.get(f"/v1/connections/{e.cid}", headers=e.h()).json()


def audits(e, action):
    return e.q("SELECT count(*) FROM audit_events WHERE tenant_id=:t AND action=:a", a=action)


def test_scheduled_health_check_marks_invalid_and_stops_syncing(selected):
    e = selected
    kinds = {d["source_kind"] for d in drain(e)}
    assert kinds == {"confluence", "connection"}  # the tick schedules both sync and health jobs
    assert scope_item(e)["freshness"] == "fresh"
    e.conf.users[fc.VALID_TOKEN].valid = False  # the token is revoked at the provider
    due(e.tenant)
    with get_engine().begin() as conn:  # the hourly health check is due
        conn.execute(
            text("UPDATE source_sync_state SET next_due_at = now() WHERE tenant_id=:t"), {"t": e.tenant}
        )
    drain(e)
    c = connection(e)
    assert c["status"] == "INVALID" and c["health"]["issues"][0]["code"] == "CREDENTIAL_INVALID"
    item = scope_item(e)
    assert item["freshness"] == "paused" and item["reason"] == "connection INVALID" and item["data_as_of"]
    due(e.tenant)
    assert all(d["source_kind"] == "connection" for d in drain(e))  # no sync jobs for an INVALID connection


def test_sync_hitting_401_invalidates_the_connection_at_once(selected):
    e = selected
    with tenant_session(e.tenant) as conn:
        queue.enqueue(conn, e.tenant, uuid.UUID(e.pid), "confluence", uuid.UUID(e.sid), "manual")
    e.conf.users[fc.VALID_TOKEN].valid = False
    [out] = drain(e, schedule=False)
    assert out["status"] == "FAILED" and out["error"] == "CREDENTIAL_INVALID"
    assert connection(e)["status"] == "INVALID" and audits(e, "connection.invalidated") == 1
    status = e.client.get(f"/v1/projects/{e.pid}/sources/sync-status", headers=e.h()).json()["sources"][0]
    assert status["last_error"]["title"] == "Reauthorization needed" and status["last_error"]["action"]


def test_stale_data_is_marked_with_data_as_of(selected):
    e = selected
    drain(e)
    with get_engine().begin() as conn:
        conn.execute(
            text(
                "UPDATE source_sync_state SET last_success_at = now() - interval '4 hours' "
                "WHERE tenant_id=:t AND source_kind='confluence'"
            ),
            {"t": e.tenant},
        )
    item = scope_item(e)
    assert item["freshness"] == "stale" and item["data_as_of"].startswith(item["data_as_of"][:10])
    status = e.client.get(f"/v1/projects/{e.pid}/sources/sync-status", headers=e.h()).json()["sources"][0]
    assert status["health"] == "stale" and status["data_as_of"]


def test_exclude_keeps_history_survives_health_and_reselect_include_catches_up(selected):
    e = selected
    drain(e)
    base = f"/v1/projects/{e.pid}/sources/confluence/{e.sid}"
    first = e.client.post(f"{base}/exclude", headers=e.h())
    assert first.status_code == 200 and first.json()["pause_cause"] == "excluded"
    assert e.client.post(f"{base}/exclude", headers=e.h()).status_code == 200  # idempotent
    assert audits(e, "source.excluded") == 1
    item = scope_item(e)
    assert (item["freshness"], item["cause"]) == ("excluded", "excluded")
    pages = e.q("SELECT count(*) FROM confluence_pages WHERE tenant_id=:t")
    assert pages == 5  # catalog and history kept
    # Neither a health check nor re-selecting the space brings it back.
    e.client.post(f"/v1/connections/{e.cid}/check", headers=e.h())
    e.select(filters={"required_labels": ["approved"]})
    assert scope_item(e)["freshness"] == "excluded"
    due(e.tenant)
    assert all(d["source_kind"] != "confluence" for d in drain(e))  # not synced while excluded
    back = e.client.post(f"{base}/include", headers=e.h()).json()
    assert back["status"] == "ACTIVE" and back["job_id"]
    drain(e, schedule=False)
    assert scope_item(e)["freshness"] == "fresh" and audits(e, "source.included") == 1
    events = e.q(
        "SELECT count(*) FROM outbox_events "
        "WHERE tenant_id=:t AND type IN ('source.excluded','source.included')"
    )
    assert events == 2


def test_revoke_keeps_a_persons_exclusion(selected):
    e = selected
    e.client.post(f"/v1/projects/{e.pid}/sources/confluence/{e.sid}/exclude", headers=e.h())
    e.client.post(f"/v1/connections/{e.cid}/revoke", headers=e.h())
    assert e.q("SELECT pause_cause FROM project_confluence_sources WHERE tenant_id=:t") == "excluded"


def test_unchanged_scheduled_health_checks_add_nothing(selected):
    e = selected
    drain(e)
    checked = audits(e, "connection.checked")
    with get_engine().begin() as conn:
        conn.execute(
            text("UPDATE source_sync_state SET next_due_at = now() WHERE tenant_id=:t"), {"t": e.tenant}
        )
    drain(e)
    assert audits(e, "connection.checked") == checked  # same status and issues: no new audit or event


def test_every_emitted_error_code_is_explained():
    app_dir = Path(__file__).resolve().parents[1] / "app"
    source = "\n".join(p.read_text() for p in app_dir.rglob("*.py"))
    patterns = (
        r'"code":\s*"([A-Z_]{4,})"',
        r'_error\(\s*"([A-Z_]{4,})"',
        r'PermanentFailure\(\s*"([A-Z_]{4,})"',
        r'code = "([A-Z_]{4,})" if',
    )
    emitted = {c for p in patterns for c in re.findall(p, source)} - {"NOT_FOUND"}
    missing = sorted(emitted - set(CATALOG))
    assert not missing, f"add plain-language entries for {missing} to app/sync/errors.py"
