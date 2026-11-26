"""INPROOM-184 (story INPROOM-30 AC 5, 8): freshness signals and conflicting or duplicate sources."""

import pytest

from tests import test_confluence_sources_integration as conf
from tests.test_document_api_integration import drain
from tests.test_documents_integration import upload
from tests.test_provenance_integration import cite, resolve

pytestmark = pytest.mark.integration
env, selected = conf.env, conf.selected


def owner(e, sql):
    from sqlalchemy import text

    from app.db import get_engine

    with get_engine().begin() as conn:
        conn.execute(text(sql), {"t": e.tenant})


def freshness(e, *citation):
    [r] = resolve(e, cite(*citation))["results"]
    return r["record"]["freshness"]


def test_each_stale_signal_and_superseded(selected):
    e = selected
    page = ("confluence", e.sid, "101")
    assert freshness(e, *page)["status"] == "current"
    owner(
        e,
        "INSERT INTO source_sync_state (tenant_id, project_id, source_kind, source_id, last_success_at, "
        "consecutive_failures) SELECT tenant_id, project_id, 'confluence', id, now(), 2 "
        "FROM project_confluence_sources WHERE tenant_id=:t",
    )
    f = freshness(e, *page)
    assert (f["status"], f["reasons"][0]["code"], f["source_sync"]["health"]) == (
        "stale",
        "SOURCE_SYNC_FAILING",
        "failing",
    )
    owner(
        e,
        "UPDATE source_sync_state SET consecutive_failures = 0, last_success_at = now() - interval '7 days' "
        "WHERE tenant_id=:t",
    )
    assert [r["code"] for r in freshness(e, *page)["reasons"]] == ["SOURCE_SYNC_STALE"]
    owner(e, "UPDATE source_sync_state SET last_success_at = now() WHERE tenant_id=:t")
    owner(
        e,
        "UPDATE confluence_pages SET current_version = current_version + 1, "
        "last_modified_at = '2099-01-01T00:00:00.000Z' WHERE tenant_id=:t AND page_id='101'",
    )
    codes = [r["code"] for r in freshness(e, *page)["reasons"]]
    assert codes == ["PROVIDER_HAS_NEWER_VERSION", "PROVIDER_CHANGED_AFTER_RETRIEVAL"]
    e.conf.edit(101, "<h1>Architecture</h1><p>v4</p>")
    assert e.sync().status_code == 200
    old = freshness(e, "confluence", e.sid, "101", 1)
    assert old["status"] == "superseded" and old["reasons"][0]["code"] == "NEWER_REVISION"


def test_conflicting_and_duplicate_sources_are_listed_if_visible(selected):
    e = selected
    a = upload(e, "a.md", b"# Architecture\n\nOne", title="Architecture").json()["document_id"]
    same = upload(e, "b.md", b"# Architecture\n\nOne", title="  ARCHITECTURE ").json()["document_id"]
    other = upload(e, "c.md", b"# Architecture\n\nTwo", title="architecture").json()["document_id"]
    [rec] = (r["record"] for r in resolve(e, cite("upload", e.pid, a))["results"])
    assert {d["external_id"] for d in rec["duplicates"]} == {same}
    conflicts = {(c["kind"], c["external_id"]) for c in rec["conflicts"]}
    assert conflicts == {("upload", other), ("confluence", "101")}  # the Confluence page differs too
    e.conf.pages[101].groups = ["bots"]  # now restricted: it must disappear from conflicts
    assert e.sync().status_code == 200
    [rec] = (r["record"] for r in resolve(e, cite("upload", e.pid, a))["results"])
    assert {c["external_id"] for c in rec["conflicts"]} == {other}


def test_documents_carry_a_provenance_summary(selected):
    e = selected
    drain(e)
    body = e.client.get(
        f"/v1/projects/{e.pid}/content/confluence/{e.sid}/101/revisions/1/document", headers=e.h()
    ).json()
    assert (
        body["provenance"]
        == {
            "freshness": "current",
            "reasons": [],
            "trust": "trusted",
            "classification": "internal",
            "derivation": "extracted",
            "retrieved_at": body["provenance"]["retrieved_at"],
        }
        and body["provenance"]["retrieved_at"]
    )
