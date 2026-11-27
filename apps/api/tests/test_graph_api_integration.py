"""INPROOM-217 (story INPROOM-34 AC 2, 4, 7): the graph read API - nodes, neighbourhoods with evidence, edges
and status - only ever shows visible content; restricted items vanish (with their links) and come back."""

import pytest

from tests import test_confluence_sources_integration as conf
from tests.test_document_api_integration import drain
from tests.test_documents_integration import upload
from tests.test_provenance_integration import resolve

pytestmark = pytest.mark.integration
env, selected = conf.env, conf.selected

NOTES = (
    b"# Release notes\n\nSee https://acme.atlassian.net/wiki/spaces/ENG/pages/101/Architecture and the "
    b"leads page /pages/103. Tracked in ABC-9."
)


def get(e, path, status=200, **params):
    r = e.client.get(f"/v1/projects/{e.pid}/graph/{path}", params=params, headers=e.h())
    assert r.status_code == status, r.text
    return r.json()


def test_neighbourhoods_status_and_visibility(selected):
    e = selected
    notes = upload(e, "notes.md", NOTES).json()["document_id"]
    lonely = upload(e, "lonely.md", b"# Lonely\n\nNothing to see.").json()["document_id"]
    drain(e)

    page = get(e, "nodes/confluence:101")
    assert page["node"]["type"] == "confluence_page" and page["node"]["label"] == "Architecture"
    assert page["node"]["items"][0]["external_id"] == "101" and page["node"]["degree"] >= 2
    [parent] = [x for x in page["outgoing"] if x["kind"] == "child_of"]
    assert (parent["to_key"], parent["origin"], parent["status"], parent["unresolved"]) == (
        "confluence:100",
        "validated",
        "active",
        False,
    )
    [mention] = [x for x in page["incoming"] if x["from_key"] == f"doc:{notes}"]
    assert mention["origin"] == "inferred" and mention["confidence"] == 0.9
    [cited] = resolve(e, mention["evidence"][0]["citation"])["results"]
    assert cited["error"] is None

    mine = get(e, f"nodes/doc:{notes}")["outgoing"]
    targets = {x["to_key"]: x for x in mine}
    assert targets["jira:ABC-9"]["unresolved"] is True  # no Jira here: an unresolved reference
    # 103 was never ingested (restricted at the source): the visible text's own mention stays unresolved
    assert targets["confluence:103"]["unresolved"] is True

    status = get(e, "status")
    assert status["pending"] == 0 and status["extractor"] == "graph@1"
    assert {"key": "jira:ABC-9", "references": 1} in status["unresolved_keys"] and status["unresolved"] >= 2
    assert f"doc:{lonely}" in status["orphan_keys"] and f"doc:{notes}" not in status["orphan_keys"]
    assert {c["name"] for c in status["origins"]} == {"validated", "inferred"}
    assert any(c["name"] == "confluence_page" for c in status["node_types"])

    docs = get(e, "nodes", type="document")["items"]
    assert {n["key"] for n in docs} == {f"doc:{notes}", f"doc:{lonely}"}
    assert [n["key"] for n in get(e, "nodes", q="Lonely")["items"]] == [f"doc:{lonely}"]
    first = get(e, "nodes", limit=2)
    second = get(e, "nodes", limit=2, cursor=first["next"])
    assert first["next"] and not {n["key"] for n in first["items"]} & {n["key"] for n in second["items"]}
    inferred = get(e, "edges", origin="inferred")["items"]
    assert inferred and all(x["origin"] == "inferred" for x in inferred)
    assert all(x["unresolved"] for x in get(e, "edges", unresolved=True)["items"])

    # restricted at the source: the page, its links and its evidence vanish; restored, they return
    e.conf.pages[101].groups = ["bots"]
    assert e.sync().status_code == 200
    drain(e)
    get(e, "nodes/confluence:101", status=404)
    assert "confluence:101" not in {x["to_key"] for x in get(e, f"nodes/doc:{notes}")["outgoing"]}
    assert all(
        "confluence:101" not in (x["from_key"], x["to_key"]) for x in get(e, "edges", limit=500)["items"]
    )
    e.conf.pages[101].groups = []
    assert e.sync().status_code == 200
    drain(e)
    assert get(e, "nodes/confluence:101")["node"]["label"] == "Architecture"

    get(e, "nodes/jira:NOPE-1", status=404)
    r = e.client.get(f"/v1/projects/{e.pid}/graph/edges", params={"origin": "guessed"}, headers=e.h())
    assert r.status_code == 422
