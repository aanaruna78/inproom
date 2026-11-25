"""INPROOM-142 (story INPROOM-25 AC 1-3): Jira reconcile emits every real change exactly once, with no
webhook at all (missed deliveries are recovered by the scheduler), and never re-emits old changes."""

import uuid

import pytest

from app.db import tenant_session
from app.sync import queue
from app.sync.handlers import plan_jira
from tests import test_jira_sources_integration as helpers
from tests.fakes import jira as fj
from tests.sync_helpers import due, events, payloads, run_sync

pytestmark = pytest.mark.integration
env = helpers.env


def _run_jira(e, **kw):
    return run_sync({"jira": e.jira.transport()}, e.tenant, **kw)


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
        "content": {"revisions": 5, "unchanged": 0, "withheld": 0},  # INPROOM-158: records stored
    }
    due(e.tenant)
    assert _run_jira(e)["result"]["changed"] == 0  # re-reading the overlap emits nothing
    _issue(e, "ABC-2")["fields"]["updated"] = "2026-11-25T10:00:00.000+0000"  # missed webhook
    _issue(e, "ABC-3")["fields"]["updated"] = "2026-11-01T10:00:00.000+0000"  # an older (out-of-order) update
    due(e.tenant)
    changed = _run_jira(e)["result"]
    assert changed["changed"] == 1 and changed["content"]["revisions"] == 1
    keys = [p["key"] for p in payloads(e, "source.jira.issue.changed")]
    assert keys.count("ABC-2") == 2 and keys.count("ABC-3") == 1  # baseline + the one real change
    # A deleted issue and a changed schema are picked up by the full reconcile.
    e.jira.projects["ABC"].issues = [i for i in e.jira.projects["ABC"].issues if i["key"] != "ABC-5"]
    e.jira.fields = [f for f in e.jira.fields if f["id"] != fj.TEAM]
    with tenant_session(e.tenant) as conn:
        queue.enqueue(conn, e.tenant, uuid.UUID(e.pid), "jira", uuid.UUID(e.sid), "manual", mode="full")
    full = _run_jira(e, schedule=False)
    assert full["result"]["deleted"] == 1 and full["result"]["schema_changed"] is True
    # ABC-3's out-of-order edit was not re-read incrementally; the full read stores it. ABC-5 is withheld.
    assert full["result"]["content"] == {"revisions": 1, "unchanged": 3, "withheld": 1}
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


def test_jira_deletion_webhook_triggers_a_full_reconcile_once(jira_selected_env, monkeypatch):
    """INPROOM-143: jira:issue_deleted enqueues a FULL job (only an id-set diff can confirm a deletion);
    a replayed or late delivery changes nothing."""
    import hashlib
    import hmac
    import json

    from app.config import get_settings

    monkeypatch.setenv("INPROOM_PUBLIC_URL", "https://inproom.example.com")
    get_settings.cache_clear()
    e = jira_selected_env
    _run_jira(e)  # baseline
    hook = e.client.post(f"/v1/connections/{e.cid}/webhook-secret", headers=e.h()).json()
    e.jira.projects["ABC"].issues = [i for i in e.jira.projects["ABC"].issues if i["key"] != "ABC-4"]
    body = json.dumps(
        {
            "webhookEvent": "jira:issue_deleted",
            "issue": {"id": "20004", "fields": {"project": {"id": "10100"}}},
        }
    ).encode()
    sig = "sha256=" + hmac.new(hook["secret"].encode(), body, hashlib.sha256).hexdigest()
    headers = {
        "x-hub-signature": sig,
        "x-atlassian-webhook-identifier": "j-1",
        "content-type": "application/json",
    }
    assert e.client.post(hook["path"], content=body, headers=headers).status_code == 202
    assert e.client.post(hook["path"], content=body, headers=headers).json()["status"] == "duplicate"
    done = _run_jira(e, schedule=False)
    assert done["mode"] == "full" and done["result"]["deleted"] == 1
    assert events(e, "source.jira.issue.deleted") == 1
