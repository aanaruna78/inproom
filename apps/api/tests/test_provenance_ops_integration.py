"""INPROOM-185 (story INPROOM-30): provenance under hostile or degraded conditions - another tenant's
citations reveal nothing, and an object-store outage degrades a record instead of failing it."""

import uuid

import pytest

from app.content import store as blobs
from tests import test_confluence_sources_integration as conf
from tests.auth_helpers import bearer
from tests.test_document_api_integration import drain
from tests.test_provenance_integration import cite, resolve

pytestmark = pytest.mark.integration
env, selected = conf.env, conf.selected


def test_another_tenants_citations_resolve_as_not_found(selected, db_client, tokens):
    e = selected
    drain(e)
    other = uuid.uuid4()
    h = bearer(tokens.mint(tenant_id=other, user_id=f"intruder-{other}", roles=["tenant_admin"]))
    ws = db_client.post("/v1/workspaces", json={"slug": "evil", "name": "Evil"}, headers=h).json()
    pid = db_client.post(
        f"/v1/workspaces/{ws['id']}/projects", json={"key": "EVL", "name": "Evil"}, headers=h
    ).json()["id"]

    class Other:
        client = db_client

    o = Other()
    o.pid = pid
    body = resolve(
        o,
        cite("confluence", e.sid, "101"),
        cite("confluence", e.sid, "101", anchor="architecture"),
        headers=h,
    )
    assert [r["error"]["code"] for r in body["results"]] == ["NOT_FOUND", "NOT_FOUND"]
    assert "Architecture" not in str(body) and "acme" not in str(body)  # no title or URL of tenant A's page
    # And tenant A's project is invisible to tenant B (indistinguishable from missing).
    foreign = db_client.post(
        f"/v1/projects/{e.pid}/provenance/resolve",
        json={"citations": [cite("confluence", e.sid, "101")]},
        headers=h,
    )
    assert foreign.status_code == 404


def test_an_object_store_outage_degrades_the_passage_only(selected, monkeypatch):
    e = selected
    drain(e)
    down = blobs.MemoryStore()
    down.fail = blobs.ObjectStoreUnavailable("connection refused")
    monkeypatch.setattr(blobs, "get_store", lambda: down)
    [result] = resolve(e, cite("confluence", e.sid, "101", anchor="architecture"))["results"]
    rec = result["error"] is None and result["record"]
    assert rec, result
    assert rec["source"]["title"] == "Architecture" and rec["passage"] is None
    assert {
        "field": "passage",
        "reason": "the parsed document is unavailable (OBJECTSTORE_UNAVAILABLE)",
    } in rec["completeness"]["missing"]
    assert rec["completeness"]["score"] < 1.0
    url = f"/v1/projects/{e.pid}/content/confluence/{e.sid}/101/revisions/1/provenance"
    r = e.client.get(url, params={"anchor": "architecture"}, headers=e.h())
    assert r.status_code == 200 and r.json()["passage"] is None
