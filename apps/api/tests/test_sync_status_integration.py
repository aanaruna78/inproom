"""INPROOM-144 (story INPROOM-25 AC 8): sync status, reconcile now and job history."""

import uuid

import pytest

from app.sync import worker
from tests import test_confluence_sources_integration as helpers
from tests.sync_helpers import due

env = helpers.env
selected = helpers.selected
pytestmark = [pytest.mark.integration, pytest.mark.usefixtures("no_content_jobs")]


def status(e, **h):
    r = e.client.get(f"/v1/projects/{e.pid}/sources/sync-status", headers=e.h(**h))
    assert r.status_code == 200, r.text
    return r.json()["sources"]


def reconcile(e, user="alice", roles=("tenant_admin",), kind="confluence", source=None):
    return e.client.post(
        f"/v1/projects/{e.pid}/sources/{kind}/{source or e.sid}/reconcile", headers=e.h(user, roles)
    )


def run(e):
    return worker.run_once("w", transports={"confluence": e.conf.transport()}, tenant=e.tenant)


def test_status_moves_from_stale_to_fresh_to_failing(selected):
    e = selected
    [before] = status(e)
    assert (before["kind"], before["name"], before["health"], before["last_success_at"]) == (
        "confluence",
        "ENG",
        "never",  # INPROOM-145: never synced is distinguished from stale
        None,
    )
    while run(e):  # the tick also queues an hourly connection health check (INPROOM-145): drain both
        pass
    [fresh] = status(e)
    assert fresh["health"] == "fresh" and fresh["last_success_at"] and not fresh["queued"]
    e.conf.failures = ["500"] * 3
    due(e.tenant)
    run(e)
    [failing] = status(e)
    assert failing["health"] == "failing" and failing["consecutive_failures"] == 1
    assert failing["last_error"]["code"] == "PROVIDER_UNAVAILABLE" and failing["queued"]


def test_reconcile_now_is_coalesced_and_listed(selected):
    e = selected
    first, second = reconcile(e), reconcile(e)
    assert first.status_code == 202 and first.json()["id"] == second.json()["id"]
    assert second.json()["triggers"] == 2 and second.json()["reason"] == "manual"
    assert status(e)[0]["queued"] is True
    jobs = e.client.get(f"/v1/projects/{e.pid}/sources/jobs", headers=e.h()).json()
    assert [(j["reason"], j["status"]) for j in jobs] == [("manual", "QUEUED")]
    run(e)
    jobs = e.client.get(f"/v1/projects/{e.pid}/sources/jobs", headers=e.h()).json()
    assert jobs[0]["status"] == "SUCCEEDED" and jobs[0]["result"]["counts"]["unchanged"] == 5


def test_reconcile_guards(selected):
    e = selected
    assert reconcile(e, user="viewer", roles=()).status_code in (403, 404)
    assert reconcile(e, source=uuid.uuid4()).status_code == 404
    assert reconcile(e, kind="jira").status_code == 404  # a Confluence id is not a Jira source
    assert (
        e.client.post(f"/v1/projects/{e.pid}/sources/svn/{e.sid}/reconcile", headers=e.h()).status_code == 422
    )
    e.client.post(f"/v1/connections/{e.cid}/revoke", headers=e.h())
    paused = reconcile(e)
    assert paused.status_code == 409 and paused.json()["code"] == "SOURCE_PAUSED"
