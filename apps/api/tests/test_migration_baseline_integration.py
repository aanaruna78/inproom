"""INPROOM-152 (story INPROOM-19 AC 6, 7): inventory review, baseline confirmation, drift, readiness."""

import pytest
from sqlalchemy import text

from app.db import get_engine
from tests import test_migration_scan_integration as scan_helpers
from tests.fakes import jira as fj

env = scan_helpers.env
mig = scan_helpers.mig
plan, scan, state = scan_helpers.plan, scan_helpers.scan, scan_helpers.state
pytestmark = pytest.mark.integration


def confirm(e, version):
    return e.client.post(
        f"/v1/projects/{e.mpid}/migration/confirm", headers=e.h(**{"If-Match": f'"{version}"'})
    )


def issues(e, **params):
    r = e.client.get(f"/v1/projects/{e.mpid}/migration/issues", params=params, headers=e.h())
    assert r.status_code == 200, r.text
    return r.json()


@pytest.fixture
def scanned(mig):
    plan(mig)
    scan(mig)
    return mig


def test_inventory_tree_and_filters(scanned):
    e = scanned
    roots = issues(e, parent="root")["items"]
    assert [(r["key"], r["type"], r["children"]) for r in roots][:3] == [
        ("LEG-1", "epic", 4),
        ("LEG-14", "epic", 4),
        ("LEG-27", "epic", 4),
    ]
    stories = issues(e, parent=roots[0]["jira_issue_id"])["items"]
    assert len(stories) == 4 and all(s["parent_key"] == "LEG-1" and s["children"] == 2 for s in stories)
    assert [o["summary"] for o in issues(e, flag="orphan")["items"]] == ["Orphan story"]
    first = issues(e, type="subtask", limit=10)
    rest = issues(e, type="subtask", limit=50, cursor=first["next_cursor"])
    assert len(first["items"]) + len(rest["items"]) == 24 and rest["next_cursor"] is None


def test_confirm_baseline_activates_the_project(scanned):
    e = scanned
    version = state(e)["plan"]["version"]
    assert e.client.post(f"/v1/projects/{e.mpid}/migration/confirm", headers=e.h()).status_code == 428
    assert confirm(e, version + 5).status_code == 412
    before = state(e)["write_readiness"]
    assert before == {"ready": False, "reasons": ["NO_BASELINE"]}
    done = confirm(e, version)
    assert done.status_code == 200, done.text
    body = done.json()
    assert (body["plan"]["status"], body["project_status"]) == ("BASELINED", "ACTIVE")
    assert (
        body["plan"]["baseline"]["counts"]["issues"] == 42
        and body["plan"]["baseline"]["confirmed_by"] == "alice"
    )
    assert body["write_readiness"] == {"ready": True, "reasons": []}
    assert e.q("SELECT count(*) FROM audit_events WHERE tenant_id=:t AND action='migration.baselined'") == 1
    again = confirm(e, body["plan"]["version"])
    assert again.status_code == 409 and again.json()["code"] == "PLAN_BASELINED"
    change = e.client.put(
        f"/v1/projects/{e.mpid}/migration",
        json={"jira_source_id": e.jsid, "history_since": "2026-01-01"},
        headers=e.h(**{"If-Match": f'"{body["plan"]["version"]}"'}),
    )
    assert change.status_code == 409 and change.json()["code"] == "PLAN_BASELINED"


def test_confirm_needs_a_fresh_completed_scan(mig):
    e = mig
    plan(e)
    missing = confirm(e, 1)
    assert missing.status_code == 409 and missing.json()["code"] == "SCAN_REQUIRED"
    scan(e)
    with get_engine().begin() as conn:
        conn.execute(
            text("UPDATE migration_runs SET finished_at = now() - interval '2 days' WHERE tenant_id=:t"),
            {"t": e.tenant},
        )
    stale = confirm(e, state(e)["plan"]["version"])
    assert stale.status_code == 409 and stale.json()["code"] == "SCAN_STALE"
    assert "SCAN_STALE" in state(e)["write_readiness"]["reasons"]


def test_drift_since_baseline_leaves_the_baseline_alone(scanned):
    e = scanned
    baseline = confirm(e, state(e)["plan"]["version"]).json()["plan"]["baseline"]
    issue = next(i for i in e.legacy.issues if i["key"] == "LEG-2")
    issue["fields"]["status"] = fj.DONE
    issue["fields"]["summary"] = "Renamed story"
    e.legacy.issues.remove(next(i for i in e.legacy.issues if i["key"] == "LEG-5"))
    extra = dict(e.legacy.issues[-1])
    e.legacy.issues.append(extra | {"id": "999123", "key": "LEG-999"})
    scan(e)
    drift = e.client.get(f"/v1/projects/{e.mpid}/migration/drift", headers=e.h()).json()
    assert drift["counts"] == {"added": 1, "changed": 1, "removed": 1}
    [changed] = drift["changed"]
    assert changed["key"] == "LEG-2" and set(changed["changes"]) == {"status", "summary"}
    assert [r["key"] for r in drift["removed"]] == ["LEG-5"] and [a["key"] for a in drift["added"]] == [
        "LEG-999"
    ]
    assert state(e)["plan"]["baseline"] == baseline  # the confirmed baseline never moves


def test_write_readiness_reports_lost_access(scanned):
    e = scanned
    confirm(e, state(e)["plan"]["version"])
    e.jira.revoke(fj.VALID_TOKEN, "LEG")
    e.client.post(f"/v1/connections/{e.cid}/check", headers=e.h())
    assert state(e)["write_readiness"] == {"ready": False, "reasons": ["SOURCE_PAUSED"]}
    drift = e.client.get(f"/v1/projects/{e.pid}/migration/drift", headers=e.h())
    assert drift.status_code == 409 and drift.json()["code"] == "NO_BASELINE"


def test_empty_inventory_baseline_carries_a_warning(mig):
    e = mig
    e.legacy.issues.clear()
    plan(e)
    scan(e)
    body = confirm(e, state(e)["plan"]["version"]).json()
    assert body["plan"]["baseline"]["warnings"] == ["EMPTY_INVENTORY"] and body["project_status"] == "ACTIVE"


def test_changed_plan_supersedes_in_flight_scan_and_needs_a_new_one(mig):
    """INPROOM-155: a scan running when the plan changes is superseded; only a scan of the new revision
    (here: a narrower history scope) can be confirmed."""
    e = mig
    plan(e)
    e.client.post(f"/v1/projects/{e.mpid}/migration/scan", headers=e.h())  # queued, not yet run
    version = state(e)["plan"]["version"]
    changed = e.client.put(
        f"/v1/projects/{e.mpid}/migration",
        json={"jira_source_id": e.jsid, "history_since": "2026-01-01"},
        headers=e.h(**{"If-Match": f'"{version}"'}),
    )
    assert changed.status_code == 200
    old = state(e)["latest_run"]
    assert old["status"] == "FAILED" and old["error"]["code"] == "PLAN_CHANGED"
    refused = confirm(e, changed.json()["plan"]["version"])
    assert refused.status_code == 409 and refused.json()["code"] == "SCAN_REQUIRED"
    assert state(e)["project_status"] == "PENDING_SOURCES"
    assert e.q("SELECT count(*) FROM audit_events WHERE tenant_id=:t AND action='migration.baselined'") == 0
    assert scan(e)["status"] == "SUCCEEDED"  # a scan of the revised plan
    ok = confirm(e, state(e)["plan"]["version"])
    assert ok.status_code == 200 and ok.json()["plan"]["baseline"]["scope"]["history_since"] == "2026-01-01"
