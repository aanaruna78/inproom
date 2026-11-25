"""QA probes for INPROOM-19 (build/qa/INPROOM-19/test_probes.py at ff1ff2b), committed as regressions.

Unfixed cases are strict xfails naming their defect:
- INPROOM-155: confirming must need a completed scan of the CURRENT plan revision (fixed);
- INPROOM-156: write-readiness must fail closed for unusable connections and archived projects.
"""

import pytest

from tests import test_migration_scan_integration as h
from tests.fakes import jira as fj

env, mig = h.env, h.mig
pytestmark = pytest.mark.integration


def confirm(e):
    v = h.state(e)["plan"]["version"]
    return e.client.post(f"/v1/projects/{e.mpid}/migration/confirm", headers=e.h(**{"If-Match": f'"{v}"'}))


@pytest.mark.parametrize("change", ["scope", "source"])
def test_changed_plan_cannot_confirm_previous_scope_scan(mig, change):
    e = mig
    h.plan(e)
    assert h.scan(e)["status"] == "SUCCEEDED"
    v = h.state(e)["plan"]["version"]
    body = {"jira_source_id": e.jsid, "history_since": "2026-11-25"}
    if change == "source":
        other = e.client.put(f"/v1/projects/{e.mpid}/sources/jira/{e.cid}:ABC", headers=e.h())
        assert other.status_code == 200
        body = {"jira_source_id": other.json()["id"]}
    changed = e.client.put(
        f"/v1/projects/{e.mpid}/migration", json=body, headers=e.h(**{"If-Match": f'"{v}"'})
    )
    assert changed.status_code == 200 and changed.json()["plan"]["status"] == "DRAFT"
    result = confirm(e)
    assert (
        result.status_code == 409
    ), f"revised plan confirmed: HTTP {result.status_code}, {result.json().get('plan', {}).get('baseline')}"


@pytest.mark.xfail(strict=True, reason="INPROOM-156: readiness ignores connection / project state")
def test_readiness_refuses_invalid_connection(mig):
    e = mig
    h.plan(e)
    assert h.scan(e)["status"] == "SUCCEEDED"
    assert confirm(e).status_code == 200
    e.jira.users[fj.VALID_TOKEN].valid = False
    check = e.client.post(f"/v1/connections/{e.cid}/check", headers=e.h())
    assert check.status_code == 200 and check.json()["status"] == "INVALID"
    ready = h.state(e)["write_readiness"]
    assert ready["ready"] is False, f"invalid credential still write-ready: {ready}"


@pytest.mark.xfail(strict=True, reason="INPROOM-156: readiness ignores connection / project state")
def test_readiness_refuses_archived_project(mig):
    e = mig
    h.plan(e)
    assert h.scan(e)["status"] == "SUCCEEDED"
    assert confirm(e).status_code == 200
    assert e.client.post(f"/v1/projects/{e.mpid}/archive", headers=e.h()).status_code == 200
    ready = h.state(e)["write_readiness"]
    assert ready["ready"] is False, f"archived project still write-ready: {ready}"


def test_rescan_keeps_baseline_and_inventory_identity(mig):
    e = mig
    h.plan(e)
    assert h.scan(e)["status"] == "SUCCEEDED"
    baseline = confirm(e).json()["plan"]["baseline"]
    before = {r["jira_issue_id"] for r in h.rows(e)}
    assert h.scan(e)["status"] == "SUCCEEDED"
    assert h.state(e)["plan"]["baseline"] == baseline
    assert {r["jira_issue_id"] for r in h.rows(e)} == before
