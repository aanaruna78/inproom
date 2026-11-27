"""INPROOM-218 (story INPROOM-34 AC 5): curators confirm, reject and correct graph edges and add manual ones;
decisions apply at once, are audited, and survive every rebuild of the graph."""

import pytest
from sqlalchemy import delete

from app.content import graph
from app.db import tenant_session
from app.db_schema import graph_sources
from tests import test_confluence_sources_integration as conf
from tests.test_document_api_integration import drain
from tests.test_documents_integration import upload

pytestmark = pytest.mark.integration
env, selected = conf.env, conf.selected

NOTES = b"# Notes\n\nDesign in /pages/101 and the runbook in /pages/102."


def get(e, path, **params):
    r = e.client.get(f"/v1/projects/{e.pid}/graph/{path}", params=params, headers=e.h())
    assert r.status_code == 200, r.text
    return r.json()


def post(e, path, body, status=200):
    r = e.client.post(f"/v1/projects/{e.pid}/graph/{path}", json=body, headers=e.h())
    assert r.status_code == status, r.text
    return r.json()


def out(e, key):
    return {(x["kind"], x["to_key"]): x for x in get(e, f"nodes/{key}")["outgoing"]}


def rebuild(e):
    with tenant_session(e.tenant) as conn:  # forget every extraction: the whole graph is rebuilt
        conn.execute(delete(graph_sources).where(graph_sources.c.project_id == e.pid))
    assert graph.run(e.tenant, e.pid)["items"] > 0


def test_review_decisions_apply_at_once_and_survive_rebuilds(selected):
    e = selected
    doc = upload(e, "notes.md", NOTES).json()["document_id"]
    other = upload(e, "other.md", b"# Other\n\nUnrelated.").json()["document_id"]
    drain(e)
    me = f"doc:{doc}"
    edges = out(e, me)
    wrong, right = edges[("mentions", "confluence:101")], edges[("mentions", "confluence:102")]

    rejected = post(e, f"edges/{wrong['id']}/review", {"action": "reject", "reason": "not the design page"})
    assert (
        rejected["edge"]["status"] == "rejected"
        and rejected["edge"]["reviewed"]["reason"] == "not the design page"
    )
    assert ("mentions", "confluence:101") not in out(e, me)  # applies at once
    listed = get(e, "edges", status="rejected")["items"]
    assert [(x["from_key"], x["to_key"]) for x in listed] == [(me, "confluence:101")]

    fixed = post(
        e, f"edges/{right['id']}/review", {"action": "correct", "to": "confluence:100", "reason": "home"}
    )
    assert fixed["edge"]["status"] == "rejected"
    corrected = fixed["corrected"]
    assert (corrected["to_key"], corrected["origin"], corrected["status"]) == (
        "confluence:100",
        "manual",
        "confirmed",
    )
    parent = get(e, "nodes/confluence:101")["outgoing"][0]  # validated: 101 child_of 100
    assert post(e, f"edges/{parent['id']}/review", {"action": "confirm", "reason": "ok"})["edge"][
        "status"
    ] == ("confirmed")

    rebuild(e)  # extraction finds the same mentions again; the decisions still hold
    after = out(e, me)
    assert ("mentions", "confluence:101") not in after and ("mentions", "confluence:102") not in after
    assert after[("mentions", "confluence:100")]["origin"] == "manual"
    parent = get(e, "nodes/confluence:101")["outgoing"][0]  # ids follow the rows: re-read after a rebuild
    assert parent["status"] == "confirmed"

    manual = {"from_key": me, "kind": "relates_to", "to_key": f"doc:{other}", "reason": "same release"}
    first = post(e, "edges", manual, status=201)
    again = post(e, "edges", manual | {"reason": "same release, restated"}, status=201)  # idempotent
    assert first["id"] == again["id"] < 0 and again["evidence"][0]["reason"] == "same release, restated"
    gone = post(e, f"edges/{first['id']}/review", {"action": "reject", "reason": "no"})
    assert gone["edge"]["status"] == "rejected" and ("relates_to", f"doc:{other}") not in out(e, me)

    for body, status, code in (
        (
            {"from_key": "confluence:101", "kind": "child_of", "to_key": "confluence:100", "reason": "x"},
            409,
            "GRAPH_EDGE_EXISTS",
        ),
        ({"from_key": me, "kind": "relates_to", "to_key": me, "reason": "x"}, 422, "GRAPH_EDGE_SELF"),
        (
            {"from_key": me, "kind": "relates_to", "to_key": "jira:NOPE-1", "reason": "x"},
            404,
            "GRAPH_NODE_NOT_FOUND",
        ),
        ({"from_key": me, "kind": "Not A Kind", "to_key": f"doc:{other}", "reason": "x"}, 422, None),
    ):
        r = post(e, "edges", body, status=status)
        assert code is None or r["code"] == code, r
    assert post(e, f"edges/{parent['id']}/review", {"action": "correct", "reason": "x"}, 422)["code"] == (
        "GRAPH_CORRECTION_TARGET_REQUIRED"
    )
    same = {"action": "correct", "to": "confluence:100", "reason": "x"}
    assert post(e, f"edges/{parent['id']}/review", same, 409)["code"] == "GRAPH_CORRECTION_SAME_TARGET"
    assert post(e, "edges/999999999/review", {"action": "confirm", "reason": "x"}, 404)["code"] == (
        "GRAPH_EDGE_NOT_FOUND"
    )

    actions = e.q(
        "SELECT string_agg(action, ',' ORDER BY occurred_at, action DESC) FROM audit_events "
        "WHERE tenant_id=:t AND action LIKE 'graph.edge.%' AND outcome='SUCCESS'"
    )
    assert sorted(actions.split(",")) == sorted(
        [
            "graph.edge.reviewed",
            "graph.edge.reviewed",
            "graph.edge.reviewed",
            "graph.edge.created",
            "graph.edge.created",
            "graph.edge.reviewed",
        ]
    ), actions
