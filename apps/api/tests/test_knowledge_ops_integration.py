"""INPROOM-189 (story INPROOM-31 AC 5, 7): forcing and observing propagation - refresh is coalesced and
audited, status says when deletions were last verified, and a failed sync leaves state and feed untouched
together."""

import pytest

from app.content import store as blobs
from app.modules import confluence_sources
from tests import test_confluence_sources_integration as conf
from tests.test_document_api_integration import drain
from tests.test_knowledge_feed_integration import feed

pytestmark = pytest.mark.integration
env, selected = conf.env, conf.selected


def refresh(e, **body):
    return e.client.post(
        f"/v1/projects/{e.pid}/knowledge/refresh", json={"reason": "audit request"} | body, headers=e.h()
    )


def status(e):
    r = e.client.get(f"/v1/projects/{e.pid}/knowledge/status", headers=e.h())
    assert r.status_code == 200, r.text
    return r.json()


def test_refresh_is_coalesced_audited_and_skips_paused_sources(selected):
    e = selected
    first = refresh(e)
    assert first.status_code == 202, first.text
    [job] = first.json()["queued"]
    assert (job["source_id"], job["mode"], job["reason"]) == (e.sid, "full", "manual")
    again = refresh(e, scope="source", kind="confluence", source_id=e.sid)
    assert again.status_code == 202 and again.json()["queued"][0]["id"] == job["id"]  # joined, not duplicated
    assert again.json()["queued"][0]["triggers"] == 2
    assert e.q("SELECT count(*) FROM audit_events WHERE tenant_id=:t AND action='knowledge.refresh'") == 2
    assert refresh(e, scope="source").status_code == 422  # a source refresh names its source
    unknown = refresh(e, scope="source", kind="jira", source_id=e.sid)
    assert unknown.status_code == 404 and unknown.json()["code"] == "SOURCE_NOT_FOUND"
    assert refresh(e, reason="").status_code == 422
    assert (
        e.client.post(f"/v1/projects/{e.pid}/sources/confluence/{e.sid}/exclude", headers=e.h()).status_code
        == 200
    )
    paused = refresh(e).json()
    assert paused["queued"] == [] and paused["skipped"] == [
        {"kind": "confluence", "source_id": e.sid, "reason": "SOURCE_PAUSED"}
    ]


def test_status_reports_the_head_last_change_and_verified_deletions(selected):
    e = selected
    before = status(e)
    [source] = before["sources"]
    assert source["pending_derivatives"] > 0  # stored by the selection sync, not yet derived
    assert source["deletions_verified_at"] is not None  # a Confluence sync lists every page
    drain(e)
    after = status(e)
    assert after["head"] == feed(e)["head"] and after["sources"][0]["pending_derivatives"] == 0
    assert after["sources"][0]["last_change_seq"] == after["head"]


def test_a_failed_sync_rolls_back_state_and_feed_together(selected, monkeypatch):
    e = selected
    drain(e)
    head = feed(e)["head"]
    e.conf.pages[102].groups = ["bots"]  # restricted: the sync withholds it
    e.conf.pages[101].add_version("<p>Architecture, revised</p>")  # and stores a new revision (object store)
    down = blobs.MemoryStore()
    down.fail = blobs.ObjectStoreUnavailable("connection refused")
    monkeypatch.setattr(blobs, "get_store", lambda: down)
    monkeypatch.setattr(confluence_sources, "get_store", lambda: down)  # the API sync's own reference
    failed = e.sync()
    assert failed.status_code == 503 and failed.json()["code"] == "OBJECTSTORE_UNAVAILABLE", failed.text
    assert failed.headers["Retry-After"] == "30"
    assert feed(e)["head"] == head  # nothing recorded: the restriction rolled back with the failed sync
    monkeypatch.undo()
    raw = e.client.get(f"/v1/projects/{e.pid}/content/confluence/{e.sid}/102/revisions/1/raw", headers=e.h())
    assert raw.status_code == 200  # and state agrees with the feed: still served once the store is back
    assert e.sync().status_code == 200
    changes = [(x["change"], x["external_id"], x["reason"]) for x in feed(e, head)["items"]]
    assert sorted(changes) == [("invalidated", "102", "RESTRICTED"), ("refreshed", "101", "NEW_REVISION")]
    assert e.sync().status_code == 200 and feed(e)["head"] == head + 2  # a retry adds nothing
