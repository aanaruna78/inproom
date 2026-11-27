"""INPROOM-215 (story INPROOM-34 AC 1, 3, 6): the graph job turns synced Jira issues, Confluence pages and
uploads into nodes and validated edges with evidence; re-runs and re-extraction never duplicate."""

import pytest
from sqlalchemy import select

from app.content import graph
from app.db import tenant_session
from app.db_schema import graph_edges, graph_nodes, graph_sources
from tests import test_confluence_sources_integration as conf
from tests import test_jira_sources_integration as jira
from tests.sync_helpers import run_sync
from tests.test_document_api_integration import drain
from tests.test_documents_integration import upload
from tests.test_provenance_integration import resolve

pytestmark = pytest.mark.integration
env, selected = conf.env, conf.selected
jira_env = jira.env


def nodes(e):
    with tenant_session(e.tenant) as conn:
        return [
            dict(r)
            for r in conn.execute(select(graph_nodes).where(graph_nodes.c.project_id == e.pid)).mappings()
        ]


def edges(e):
    with tenant_session(e.tenant) as conn:
        return [
            dict(r)
            for r in conn.execute(select(graph_edges).where(graph_edges.c.project_id == e.pid)).mappings()
        ]


def triples(e):
    return {(x["from_key"], x["kind"], x["to_key"]) for x in edges(e)}


def test_jira_issues_become_nodes_and_validated_edges(jira_env):
    e = jira_env
    issues = e.jira.projects["ABC"].issues
    issues[1]["fields"]["components"] = [{"name": "Billing"}]
    issues[1]["fields"]["fixVersions"] = [{"name": "4.2"}]
    issues[2]["fields"]["components"] = [{"name": "Billing"}]
    e.cid = e.connect().json()["id"]
    e.sid = e.select().json()["id"]
    run_sync({"jira": e.jira.transport()}, e.tenant)
    drain(e)

    items = {n["key"] for n in nodes(e) if n["role"] == "item"}
    assert items == {f"jira:ABC-{i}" for i in range(1, 6)}
    issue = next(n for n in nodes(e) if n["key"] == "jira:ABC-2")
    assert issue["type"] == "jira_issue" and issue["label"] == "ABC-2: Create invoice"
    assert issue["attributes"]["issue_type"] == "story"
    # one component node, asserted by both issues that name it (two occurrences, one key)
    billing = [n for n in nodes(e) if n["key"] == "component:ABC:Billing"]
    assert len(billing) == 2 and {n["role"] for n in billing} == {"derived"}
    assert triples(e) >= {
        ("jira:ABC-2", "child_of", "jira:ABC-1"),
        ("jira:ABC-3", "child_of", "jira:ABC-1"),
        ("jira:ABC-4", "child_of", "jira:ABC-3"),
        ("jira:ABC-2", "blocks", "jira:ABC-3"),
        ("jira:ABC-3", "relates", "jira:ABC-5"),
        ("jira:ABC-2", "in_component", "component:ABC:Billing"),
        ("jira:ABC-2", "fix_version", "version:ABC:4.2"),
    }
    blocks = [x for x in edges(e) if x["kind"] == "blocks"]
    assert len(blocks) == 2  # both ends' records state it: one natural edge, evidence from each
    for b in blocks:
        assert b["origin"] == "validated" and b["confidence"] == 1.0 and b["extractor"] == graph.EXTRACTOR
        [ev] = b["evidence"]
        assert ev["field"] == "issuelinks" and ev["citation"]["kind"] == "jira" and ev["citation"]["seq"] == 1

    before = (len(nodes(e)), len(edges(e)))
    assert graph.run(e.tenant, e.pid)["items"] == 0  # nothing changed: nothing re-extracted
    with tenant_session(e.tenant) as conn:  # forget the bookkeeping: a full re-extraction duplicates nothing
        conn.execute(graph_sources.delete().where(graph_sources.c.project_id == e.pid))
    assert graph.run(e.tenant, e.pid)["items"] == 5
    assert (len(nodes(e)), len(edges(e))) == before


def test_confluence_pages_attachments_and_uploads(selected):
    e = selected
    doc = upload(e, "notes.md", b"# Notes\n\nPlain notes.").json()["document_id"]
    drain(e)
    by_key = {n["key"]: n for n in nodes(e) if n["role"] == "item"}
    assert (
        by_key["confluence:101"]["type"] == "confluence_page"
        and by_key["confluence:101"]["label"] == "Architecture"
    )
    assert by_key[f"doc:{doc}"]["type"] == "document"
    assert {
        ("confluence:101", "child_of", "confluence:100"),
        ("confluence:102", "child_of", "confluence:100"),
    } <= triples(e)
    attachments = [k for k in by_key if k.startswith("confluence:att")]
    for key in attachments:
        assert (key, "attached_to", "confluence:101") in triples(e)

    # a new revision is re-extracted in place: the item's rows are replaced, never added to
    old = [x for x in edges(e) if x["from_key"] == "confluence:101"]
    seq = old[0]["evidence"][0]["citation"]["seq"]
    e.conf.pages[101].add_version("<h1>Architecture</h1><p>v4</p>")
    assert e.sync().status_code == 200
    drain(e)
    mine = [x for x in edges(e) if x["from_key"] == "confluence:101"]
    assert len(mine) == len(old) and all(
        ev["citation"]["seq"] == seq + 1 for x in mine for ev in x["evidence"]
    )
    with tenant_session(e.tenant) as conn:
        assert (
            conn.execute(select(graph_sources.c.extractor).where(graph_sources.c.project_id == e.pid))
            .scalars()
            .all()
        )


MENTIONS = (
    b"# Invoicing notes\n\nThe PDF work is tracked in ABC-2 and blocked by ABC-3. "
    b"Legacy ticket OLD-9 is gone. "
    b"Architecture: https://acme.atlassian.net/wiki/spaces/ENG/pages/101/Architecture"
)


def test_mentions_become_inferred_edges_with_citable_evidence(jira_env):
    e = jira_env
    e.cid = e.connect().json()["id"]
    e.sid = e.select().json()["id"]
    run_sync({"jira": e.jira.transport()}, e.tenant)
    doc = upload(e, "invoicing.md", MENTIONS).json()["document_id"]
    twin = upload(e, "invoicing-copy.md", MENTIONS).json()["document_id"]
    drain(e)

    mine = {x["to_key"]: x for x in edges(e) if x["from_key"] == f"doc:{doc}" and x["kind"] == "mentions"}
    assert mine["jira:ABC-2"]["confidence"] == 0.9 and mine["jira:ABC-3"]["origin"] == "inferred"
    assert mine["jira:OLD-9"]["confidence"] == 0.5  # unknown prefix: kept, unresolved (no node carries it)
    assert mine["confluence:101"]["confidence"] == 0.9  # no Confluence here: an unresolved reference too
    [ev] = mine["jira:ABC-2"]["evidence"]
    assert ev["text"] == "ABC-2" and "tracked in ABC-2" in ev["excerpt"]
    assert ev["citation"]["kind"] == "upload" and ev["citation"]["external_id"] == doc
    [cited] = resolve(e, ev["citation"])["results"]  # the evidence resolves through provenance
    assert cited["error"] is None
    keys = {n["key"] for n in nodes(e)}
    assert "jira:ABC-2" in keys and "jira:OLD-9" not in keys  # nothing invented

    a, b = sorted((f"doc:{doc}", f"doc:{twin}"))
    dupes = [x for x in edges(e) if x["kind"] == "duplicates"]
    assert {(x["from_key"], x["to_key"]) for x in dupes} == {(a, b)}  # one natural edge (stated by both)
    assert all(x["confidence"] == 0.95 and x["evidence"][0]["field"] == "content_sha256" for x in dupes)
    # issues mention each other too: their descriptions are chunks like any other
    assert not [x for x in edges(e) if x["kind"] == "mentions" and x["from_key"] == x["to_key"]]


def test_a_changed_twin_drops_the_duplicates_edge_everywhere(selected):
    """INPROOM-222: when one of two identical documents changes, the pair is no longer a duplicate - in the
    stored graph too, not only in what is served - and unchanged twins do not re-check each other forever."""
    e = selected
    body = b"# Same\n\nIdentical content."
    a = upload(e, "first.md", body).json()["document_id"]
    b = upload(e, "second.md", body).json()["document_id"]
    c = upload(e, "third.md", body).json()["document_id"]
    drain(e)
    assert len({(x["from_key"], x["to_key"]) for x in edges(e) if x["kind"] == "duplicates"}) == 3
    assert graph.run(e.tenant, e.pid) == {"items": 0, "nodes": 0, "edges": 0, "dropped": 0, "more": False}
    r = e.client.put(
        f"/v1/projects/{e.pid}/documents/{a}",
        files={"file": ("first.md", b"# Different\n\nNow different.")},
        headers=e.h(**{"If-Match": '"1"'}),
    )
    assert r.status_code == 200, r.text
    drain(e)
    pairs = {tuple(sorted((x["from_key"], x["to_key"]))) for x in edges(e) if x["kind"] == "duplicates"}
    assert pairs == {tuple(sorted((f"doc:{b}", f"doc:{c}")))}  # the stored rows converged
    assert graph.run(e.tenant, e.pid)["items"] == 0  # and settled
