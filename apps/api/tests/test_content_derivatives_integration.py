"""INPROOM-159 (story INPROOM-27 AC 1, 3): derivatives name their canonical revision; a new revision
supersedes them; invalidation rebuilds them idempotently."""

import json

import pytest
from sqlalchemy import select

from app.content import derivatives
from app.content import store as blobs
from app.db import tenant_session
from app.db_schema import derivatives as table
from app.sync import worker
from tests import test_confluence_sources_integration as conf
from tests.sync_helpers import due

pytestmark = pytest.mark.integration
env = conf.env
selected = conf.selected


def drain(e):
    """Run every runnable job (derive jobs included); returns the derive results."""
    results = []
    while (
        out := worker.run_once("w", transports={"confluence": e.conf.transport()}, tenant=e.tenant)
    ) is not None:
        if out["source_kind"] == "derive":
            results.append(out)
    return results


def derived(e, page_id, seq, producer="text"):
    """The revision's derivatives of one producer (these tests exercise the contract through text@1)."""
    url = f"/v1/projects/{e.pid}/content/confluence/{e.sid}/{page_id}/revisions/{seq}/derivatives"
    body = e.client.get(url, headers=e.h()).json()
    return body | {"items": [d for d in body["items"] if d["producer"] == producer]}


def invalidate(e, **body):
    return e.client.post(
        f"/v1/projects/{e.pid}/content/invalidate",
        json={"kind": "confluence", "source_id": e.sid, "reason": "parser fix"} | body,
        headers=e.h(),
    )


def text_of(e, derivative):
    with tenant_session(e.tenant) as conn:
        key = conn.execute(select(table.c.blob_key).where(table.c.id == derivative["id"])).scalar_one()
    return blobs.read(blobs.get_store(), key).decode()


def live(e):
    with tenant_session(e.tenant) as conn:
        return derivatives.live(conn, e.pid, kind="text")


def test_each_revision_gets_its_derivative_and_a_new_revision_supersedes_it(selected):
    e = selected
    assert derived(e, "101", 1)["items"][0]["status"] == "PENDING"  # queued by the sync that stored it
    [run] = drain(e)
    assert run["status"] == "SUCCEEDED" and run["result"] == {
        "built": 6,  # parse@1 and text@1 for each of the three pages
        "failed": 0,
        "skipped": 0,
        "failures": {},
        "symbols": 0,  # symbols projected for search (code only, INPROOM-176)
        "more": False,
    }
    first = derived(e, "101", 1)
    [d1] = first["items"]
    assert (d1["kind"], d1["producer"], d1["producer_version"], d1["status"]) == ("text", "text", 1, "READY")
    assert text_of(e, d1).startswith("Architecture\nServices:\napi\nweb\n")
    assert sorted(x["external_id"] for x in live(e)) == ["100", "101", "102"]

    e.conf.edit(101, "<h1>Architecture</h1><p>Three services.</p>")
    assert e.sync().status_code == 200
    assert derived(e, "101", 1)["items"][0]["status"] == "SUPERSEDED"
    drain(e)
    [d2] = derived(e, "101", 2)["items"]
    assert d2["status"] == "READY" and text_of(e, d2) == "Architecture\nThree services.\n"
    current = {x["external_id"]: x["seq"] for x in live(e)}
    assert current == {"100": 1, "101": 2, "102": 1}  # consumers only see the current revision's output


def test_invalidation_rebuilds_identically_and_never_duplicates_pending_work(selected):
    e = selected
    drain(e)
    before = derived(e, "101", 1)["items"][0]
    r = invalidate(e, external_id="101", producer="text")
    assert r.status_code == 202, r.text
    assert (r.json()["invalidated"], r.json()["queued"], r.json()["pending"]) == (1, 1, 1)
    again = invalidate(
        e, external_id="101", producer="text"
    ).json()  # nothing built yet: nothing new is queued
    assert (again["invalidated"], again["queued"], again["pending"]) == (0, 0, 1)
    assert [x["external_id"] for x in live(e)] == ["100", "102"]  # invalidated output is not served
    drain(e)
    items = derived(e, "101", 1)["items"]
    assert [x["status"] for x in items] == ["INVALIDATED", "READY"]
    assert items[1]["output_sha256"] == before["output_sha256"]  # same revision, same producer => same bytes
    whole = invalidate(e).json()
    assert (whole["objects"], whole["invalidated"]) == (3, 6)  # both producers of all three pages
    assert e.q("SELECT count(*) FROM audit_events WHERE tenant_id=:t AND action='content.invalidate'") == 3
    assert (
        e.q(
            "SELECT count(*) FROM outbox_events WHERE tenant_id=:t AND type='content.derivatives.invalidated'"
        )
        == 3
    )


def test_invalidation_input_is_checked(selected):
    e = selected
    assert invalidate(e, producer="ocr").json()["code"] == "UNKNOWN_PRODUCER"
    assert invalidate(e, external_id="999").json()["code"] == "CONTENT_NOT_FOUND"
    assert invalidate(e, reason="").status_code == 422


def test_corrupt_input_fails_the_derivative_and_an_outage_retries_the_job(selected, monkeypatch):
    e = selected
    store = blobs.get_store()
    rev = e.client.get(f"/v1/projects/{e.pid}/content/confluence/{e.sid}/102", headers=e.h()).json()
    key = blobs.blob_key(e.tenant, rev["current"]["content_sha256"])
    original = blobs.read(store, key)
    store.client.put_object(Bucket=store.bucket, Key=key, Body=b"tampered", Metadata={"sha256": "x"})
    try:
        down = blobs.MemoryStore()
        down.fail = blobs.ObjectStoreUnavailable("connection refused")
        monkeypatch.setattr(blobs, "get_store", lambda: down)
        [outage] = drain(e)
        assert outage["status"] == "QUEUED" and outage["error"] == "OBJECTSTORE_UNAVAILABLE"
        assert derived(e, "101", 1)["items"][0]["status"] == "PENDING"  # nothing half-done
        monkeypatch.undo()
        due(e.tenant)
        drain(e)
        [bad] = derived(e, "102", 1)["items"]
        assert bad["status"] == "FAILED" and bad["error"]["code"] == "BLOB_CORRUPT"
        assert derived(e, "101", 1)["items"][0]["status"] == "READY"
    finally:
        store.put_if_absent(key, original, "text/plain")


def test_a_synced_page_gets_an_anchored_parse_of_its_revision(selected):
    """INPROOM-164: parse@1 runs as a derivative of every stored Confluence revision."""
    e = selected
    drain(e)
    [parsed] = derived(e, "101", 1, producer="parse")["items"]
    assert (parsed["kind"], parsed["producer_version"], parsed["status"]) == ("document", 2, "READY")
    doc = json.loads(text_of(e, parsed))
    assert doc["format"] == "confluence" and doc["sections"][0]["anchor"] == "architecture"
    assert doc["sections"][0]["heading"] == "Architecture" and "api" in doc["sections"][0]["text"]
