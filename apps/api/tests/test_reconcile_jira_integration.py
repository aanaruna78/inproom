"""INPROOM-142 (story INPROOM-25 AC 1-3): Jira reconcile emits every real change exactly once, with no
webhook at all (missed deliveries are recovered by the scheduler), and never re-emits old changes."""

import uuid

import pytest

from app.db import tenant_session
from app.sync import queue, worker
from app.sync.handlers import plan_jira
from tests import test_jira_sources_integration as helpers
from tests.fakes import jira as fj
from tests.sync_helpers import due, events, payloads

pytestmark = pytest.mark.integration
env = helpers.env


def _run_jira(e, **kw):
    return worker.run_once("w", transports={"jira": e.jira.transport()}, tenant=e.tenant, **kw)


def _issue(e, key):
    return next(i for i in e.jira.projects["ABC"].issues if i["key"] == key)


@pytest.fixture
def jira_selected_env(env):
    e = env
    e.cid = e.connect().json()["id"]
    e.sid = e.select().json()["id"]
    return e


def test_jira_updates_deletions_and_schema_once(jira_selected_env):
    e = jira_selected_env
    first = _run_jira(e)
    assert first["result"] == {
        "changed": 5,
        "deleted": 0,
        "schema_changed": False,
        "full": True,
        "ids_tracked": True,
    }
    due(e.tenant)
    assert _run_jira(e)["result"]["changed"] == 0  # re-reading the overlap emits nothing
    _issue(e, "ABC-2")["fields"]["updated"] = "2026-11-25T10:00:00.000+0000"  # missed webhook
    _issue(e, "ABC-3")["fields"]["updated"] = "2026-11-01T10:00:00.000+0000"  # an older (out-of-order) update
    due(e.tenant)
    assert _run_jira(e)["result"]["changed"] == 1
    keys = [p["key"] for p in payloads(e, "source.jira.issue.changed")]
    assert keys.count("ABC-2") == 2 and keys.count("ABC-3") == 1  # baseline + the one real change
    # A deleted issue and a changed schema are picked up by the full reconcile.
    e.jira.projects["ABC"].issues = [i for i in e.jira.projects["ABC"].issues if i["key"] != "ABC-5"]
    e.jira.fields = [f for f in e.jira.fields if f["id"] != fj.TEAM]
    with tenant_session(e.tenant) as conn:
        queue.enqueue(conn, e.tenant, uuid.UUID(e.pid), "jira", uuid.UUID(e.sid), "manual", mode="full")
    full = _run_jira(e, schedule=False)
    assert full["result"]["deleted"] == 1 and full["result"]["schema_changed"] is True
    assert [p["issue_id"] for p in payloads(e, "source.jira.issue.deleted")] == ["20005"]
    assert events(e, "source.jira.schema.changed") == 1
    with tenant_session(e.tenant) as conn:
        queue.enqueue(conn, e.tenant, uuid.UUID(e.pid), "jira", uuid.UUID(e.sid), "manual", mode="full")
    again = _run_jira(e, schedule=False)
    assert again["result"]["deleted"] == 0 and again["result"]["schema_changed"] is False


def test_jira_watermark_planning():
    def issue(i, updated):
        return {"id": i, "key": f"K-{i}", "fields": {"updated": updated}}

    t1, t2 = "2026-11-20T10:00:00.000+0000", "2026-11-20T10:00:30.000+0000"
    changed, _, mark = plan_jira({}, [issue("1", t1), issue("2", t2)], full=False)
    assert [c["id"] for c in changed] == ["1", "2"] and mark["updated"] == t2 and mark["seen"] == ["2"]
    # Overlap re-read: same timestamp, unseen id -> change; seen id -> not.
    changed, _, mark = plan_jira(mark, [issue("2", t2), issue("3", t2), issue("1", t1)], full=False)
    assert [c["id"] for c in changed] == ["3"] and mark["seen"] == ["2", "3"]
    changed, deleted, mark = plan_jira(
        mark | {"ids": ["1", "2", "3"]}, [issue("1", t1), issue("2", t2)], full=True
    )
    assert changed == [] and deleted == ["3"] and mark["ids"] == ["1", "2"]
