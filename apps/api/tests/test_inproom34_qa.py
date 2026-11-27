"""QA probes for INPROOM-34: superseded evidence (INPROOM-221), a stale duplicates edge (INPROOM-222) and a
purged manual-edge endpoint (INPROOM-223). Committed from QA's tests/qa_inproom3334.py."""

from datetime import UTC, datetime, timedelta

import pytest

from app.content import retention, store
from tests import test_confluence_sources_integration as conf
from tests.test_document_api_integration import drain
from tests.test_documents_integration import upload
from tests.test_graph_api_integration import get
from tests.test_graph_review_integration import post

env, selected = conf.env, conf.selected
pytestmark = pytest.mark.integration


def replace(e, doc, data):
    response = e.client.put(
        f"/v1/projects/{e.pid}/documents/{doc}",
        files={"file": ("notes.md", data)},
        headers=e.h(**{"If-Match": '"1"'}),
    )
    assert response.status_code == 200, response.text
    assert response.json()["seq"] == 2


def test_graph_never_returns_evidence_from_superseded_revision(selected):
    e = selected
    doc = upload(e, "notes.md", b"# Notes\n\nSee /pages/101.").json()["document_id"]
    drain(e)
    assert get(e, f"nodes/doc:{doc}")["outgoing"]
    replace(e, doc, b"# Notes\n\nNo page references remain.")
    # Check the committed read boundary before asynchronous extraction catches up.
    outgoing = get(e, "edges")["items"]
    obsolete = [
        x
        for x in outgoing
        if x["from_key"] == f"doc:{doc}" and any(v.get("citation", {}).get("seq") == 1 for v in x["evidence"])
    ]
    assert obsolete == [], obsolete


def test_duplicate_edge_is_removed_after_one_twin_changes(selected):
    e = selected
    body = b"# Same\n\nIdentical content."
    a = upload(e, "first.md", body).json()["document_id"]
    b = upload(e, "second.md", body).json()["document_id"]
    drain(e)
    assert get(e, "edges", kind="duplicates")["items"]
    replace(e, a, b"# Different\n\nThis document now has different content.")
    drain(e)
    twins = {f"doc:{a}", f"doc:{b}"}
    obsolete = [
        x for x in get(e, "edges", kind="duplicates")["items"] if {x["from_key"], x["to_key"]} == twins
    ]
    assert obsolete == [], obsolete


def test_purged_manual_edge_endpoint_is_not_resurrected_as_unresolved(selected):
    e = selected
    a = upload(e, "first.md", b"# A\n\nFirst.").json()["document_id"]
    b = upload(e, "second.md", b"# B\n\nSecond.").json()["document_id"]
    drain(e)
    post(
        e,
        "edges",
        {"from_key": f"doc:{a}", "kind": "relates_to", "to_key": f"doc:{b}", "reason": "same project"},
        status=201,
    )
    assert e.client.delete(f"/v1/projects/{e.pid}/documents/{b}", headers=e.h()).status_code == 204
    assert get(e, "edges", origin="manual")["items"] == []
    result = retention.purge_project(
        e.tenant, e.pid, store.get_store(), datetime.now(UTC) + timedelta(days=3660)
    )
    assert result["revisions_purged"] >= 1
    leaked = [x for x in get(e, "edges", origin="manual")["items"] if x["to_key"] == f"doc:{b}"]
    assert leaked == [], leaked
