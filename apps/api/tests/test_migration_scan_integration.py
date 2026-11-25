"""INPROOM-151 (story INPROOM-19 AC 1-5): migration plan and resumable Jira inventory scan."""

import pytest

from app.sync import worker
from tests import test_jira_sources_integration as helpers
from tests.fakes import jira as fj
from tests.sync_helpers import due

env = helpers.env
pytestmark = pytest.mark.integration


@pytest.fixture
def mig(env):
    """A MIGRATION project whose selected Jira project is the legacy fake project LEG."""
    e = env
    e.legacy = fj.legacy_project(e.jira)
    e.cid = e.connect().json()["id"]
    ws = e.client.get("/v1/workspaces", headers=e.h()).json()["items"][0]["id"]
    project = e.client.post(
        f"/v1/workspaces/{ws}/projects",
        json={"key": "OLD", "name": "Legacy", "mode": "MIGRATION"},
        headers=e.h(),
    ).json()
    assert project["status"] == "PENDING_SOURCES"
    e.mpid = project["id"]
    r = e.client.put(f"/v1/projects/{e.mpid}/sources/jira/{e.cid}:LEG", headers=e.h())
    assert r.status_code == 200, r.text
    e.jsid = r.json()["id"]
    return e


def plan(e, **body):
    return e.client.put(
        f"/v1/projects/{e.mpid}/migration", json={"jira_source_id": e.jsid} | body, headers=e.h()
    )


def scan(e):
    """Request a scan and run worker jobs until the migration job reports (or nothing is left)."""
    r = e.client.post(f"/v1/projects/{e.mpid}/migration/scan", headers=e.h())
    assert r.status_code == 202, r.text
    return work(e)


def work(e):
    while (out := worker.run_once("w", transports={"jira": e.jira.transport()}, tenant=e.tenant)) is not None:
        if out["source_kind"] == "migration":
            return out
    return None


def state(e):
    return e.client.get(f"/v1/projects/{e.mpid}/migration", headers=e.h()).json()


def rows(e, where="true"):
    from sqlalchemy import text

    from app.db import get_engine

    with get_engine().connect() as conn:
        return [
            dict(r)
            for r in conn.execute(
                text(f"SELECT * FROM migration_issues WHERE tenant_id=:t AND {where} ORDER BY key"),
                {"t": e.tenant},
            ).mappings()
        ]


def test_inventory_keeps_jira_as_it_is_and_reports_what_it_cannot_resolve(mig):
    e = mig
    assert plan(e).status_code == 200
    out = scan(e)
    assert out["status"] == "SUCCEEDED" and out["result"]["issues"] == 42
    run = state(e)["latest_run"]
    assert run["status"] == "SUCCEEDED" and run["counts"]["by_type"] == {
        "epic": 3,
        "other": 1,
        "story": 14,
        "subtask": 24,
    }
    assert run["counts"]["flags"] == {"orphan": 1, "unresolved_link": 1, "unsupported_type": 1}
    assert run["scanned_as"]["account"] == fj.EMAIL and run["scanned_as"]["permissions"]["BROWSE_PROJECTS"]
    assert run["diagnostics"]["orphan"][0]["parent_key"] == "OLD-1"  # reported, never invented
    assert run["diagnostics"]["unresolved_link"][0]["targets"] == ["OTHER-7"]
    epic = next(r for r in rows(e) if r["key"] == "LEG-1")
    story = next(r for r in rows(e) if r["key"] == "LEG-2")
    assert (epic["type"], epic["original_key"], story["parent_key"], story["parent_issue_id"]) == (
        "epic",
        "LEG-1",
        "LEG-1",
        epic["jira_issue_id"],
    )
    assert (story["creator"], story["reporter"]) == ("Ada", "Grace")
    assert state(e)["plan"]["status"] == "SCANNED"


def test_empty_project_scans_cleanly(mig):
    e = mig
    e.legacy.issues.clear()
    plan(e)
    assert scan(e)["result"]["issues"] == 0 and state(e)["latest_run"]["counts"]["issues"] == 0


def test_large_scan_interrupted_mid_way_resumes_exactly(mig):
    e = mig
    e.jira.projects.pop("LEG")
    e.legacy = fj.legacy_project(e.jira, epics=43, stories=9, subtasks=2)  # 1,207 issues, 13 pages of 100
    plan(e)
    e.jira.fail_searches_from = e.jira.search_calls + 7  # pages 1-6 succeed, page 7 hits an outage
    first = scan(e)
    assert first["status"] == "QUEUED"  # the worker will retry
    mid = state(e)["latest_run"]
    assert (mid["status"], mid["pages_done"], mid["issues_seen"]) == ("RUNNING", 6, 600)
    assert len(rows(e)) == 600
    e.jira.fail_searches_from = None  # the outage ends
    due(e.tenant)
    done = work(e)
    assert done["status"] == "SUCCEEDED" and done["result"]["issues"] == 1207
    final = state(e)["latest_run"]
    assert (final["pages_done"], final["issues_seen"]) == (13, 1207) and len(rows(e)) == 1207


def test_rescan_is_idempotent_tracks_renames_and_removals_only_when_complete(mig):
    e = mig
    plan(e)
    scan(e)
    renamed = next(i for i in e.legacy.issues if i["key"] == "LEG-2")
    renamed["key"] = "LEG-900"  # e.g. the project was re-keyed / the issue moved within it
    gone = next(i for i in e.legacy.issues if i["key"] == "LEG-3")
    e.legacy.issues.remove(gone)  # deleted or moved to another project
    assert scan(e)["result"] == {"issues": 41, "removed": 1, "run_id": state(e)["latest_run"]["id"]}
    moved = rows(e, "key = 'LEG-900'")[0]
    assert moved["original_key"] == "LEG-2" and "key_changed" in moved["flags"]
    assert rows(e, "key = 'LEG-3'")[0]["present"] is False  # history kept
    assert len(rows(e)) == 42  # nothing duplicated
    # An interrupted scan never removes anything, even if it did not see an issue.
    e.legacy.issues.remove(next(i for i in e.legacy.issues if i["key"] == "LEG-4"))
    e.jira.fail_searches_from = e.jira.search_calls + 1
    assert scan(e)["status"] == "QUEUED"
    assert rows(e, "key = 'LEG-4'")[0]["present"] is True


def test_plan_and_scan_guards(mig):
    e = mig
    regular = e.client.put(f"/v1/projects/{e.pid}/migration", json={"jira_source_id": e.jsid}, headers=e.h())
    assert regular.status_code == 409 and regular.json()["code"] == "NOT_A_MIGRATION_PROJECT"
    first_scan = e.client.post(f"/v1/projects/{e.mpid}/migration/scan", headers=e.h())
    assert first_scan.status_code == 409 and first_scan.json()["code"] == "PLAN_REQUIRED"
    other = e.client.put(f"/v1/projects/{e.pid}/sources/jira/{e.cid}:ABC", headers=e.h()).json()["id"]
    foreign = plan(e, jira_source_id=other)  # selected for a different project
    assert foreign.status_code == 422 and foreign.json()["code"] == "JIRA_SOURCE_NOT_SELECTED"
    created = plan(e)
    assert created.status_code == 200 and created.headers["etag"] == '"1"'
    assert plan(e, history_since="2026-01-01").status_code == 428
    stale = e.client.put(
        f"/v1/projects/{e.mpid}/migration",
        json={"jira_source_id": e.jsid},
        headers=e.h(**{"If-Match": '"9"'}),
    )
    assert stale.status_code == 412
    assert e.client.post(f"/v1/projects/{e.mpid}/migration/scan", headers=e.h("v", ())).status_code in (
        403,
        404,
    )
