"""INPROOM-186 (story INPROOM-31 AC 1, 2, 3, 5): the knowledge change feed - every invalidation, restoration
and refresh is one ordered entry, written in the same commit as the state change, never duplicated."""

import pytest
from sqlalchemy import text

from tests import test_confluence_sources_integration as conf
from tests.test_document_api_integration import drain
from tests.test_documents_integration import settings, upload

pytestmark = pytest.mark.integration
env, selected = conf.env, conf.selected


def feed(e, after=0, limit=500):
    r = e.client.get(
        f"/v1/projects/{e.pid}/knowledge/changes", params={"after": after, "limit": limit}, headers=e.h()
    )
    assert r.status_code == 200, r.text
    return r.json()


def entries(e, after):
    return [(x["change"], x["scope"], x["external_id"], x["reason"]) for x in feed(e, after)["items"]]


def head(e):
    return feed(e)["head"]


def test_item_changes_are_recorded_once_in_order(selected, owner_engine):
    e = selected
    drain(e)
    first = feed(e)
    assert {x["change"] for x in first["items"]} == {"refreshed"}  # every stored page, NEW_REVISION seq 1
    assert [x["seq"] for x in first["items"]] == list(range(1, first["head"] + 1))  # gap-free
    assert all(x["revision_seq"] == 1 for x in first["items"])
    events = e.events("knowledge.changed")

    start = head(e)
    e.conf.pages[102].groups = ["bots"]  # restricted at the source
    assert e.sync().status_code == 200
    assert entries(e, start) == [("invalidated", "item", "102", "RESTRICTED")]
    assert e.events("knowledge.changed") == events + 1  # one event per transaction
    assert e.sync().status_code == 200  # re-sync: nothing changed, nothing appended
    assert head(e) == start + 1

    start = head(e)
    e.conf.pages[102].groups = []
    e.conf.pages[101].status = "trashed"
    assert e.sync().status_code == 200
    changes = entries(e, start)
    assert ("restored", "item", "102", "RESTRICTED") in changes
    assert ("invalidated", "item", "101", "DELETED") in changes
    start = head(e)
    with owner_engine.begin() as conn:  # what retention does when the deletion window ends
        conn.execute(
            text(
                "UPDATE source_revisions r SET purged_at = now(), blob_key = NULL FROM source_objects o "
                "WHERE r.object_id = o.id AND o.tenant_id = :t AND o.external_id = '101'"
            ),
            {"t": e.tenant},
        )
    assert entries(e, start) == [("invalidated", "item", "101", "PURGED")]
    body = str(feed(e, start))
    assert "Runbooks" not in body and "Architecture" not in body and "atlassian" not in body  # ids only


def test_source_and_setting_changes_are_one_entry_each_and_reversible(selected):
    e = selected
    drain(e)
    base = f"/v1/projects/{e.pid}/sources/confluence/{e.sid}"
    start = head(e)
    assert e.client.post(f"{base}/exclude", headers=e.h()).status_code == 200
    assert e.client.post(f"{base}/exclude", headers=e.h()).status_code == 200  # idempotent: no entry
    assert e.client.post(f"{base}/include", headers=e.h()).status_code == 200
    assert entries(e, start) == [
        ("invalidated", "source", None, "SOURCE_PAUSED"),
        ("restored", "source", None, "SOURCE_PAUSED"),
    ]

    start = head(e)
    policy = f"{base}/policy"
    put = e.client.put(
        policy,
        json={"tier": "trusted", "classification": "confidential", "reason": "contains secrets"},
        headers=e.h(**{"If-Match": '"0"'}),
    )
    assert put.status_code == 200, put.text
    settings(e, max_classification="confidential")  # the cap is raised: the source is servable again
    assert entries(e, start) == [
        ("invalidated", "source", None, "CLASSIFICATION_EXCEEDS_PROJECT"),
        ("restored", "source", None, "CLASSIFICATION_EXCEEDS_PROJECT"),
    ]

    start = head(e)
    doc = upload(e, "notes.md", b"# Notes").json()["document_id"]
    settings(e, allowed_kinds=["git", "jira", "confluence"])
    settings(e, allowed_kinds=["git", "jira", "confluence", "upload"])
    assert e.client.delete(f"/v1/projects/{e.pid}/documents/{doc}", headers=e.h()).status_code in (200, 204)
    assert entries(e, start) == [
        ("refreshed", "item", doc, "NEW_REVISION"),
        ("invalidated", "source", None, "UPLOADS_DISABLED"),
        ("restored", "source", None, "UPLOADS_DISABLED"),
        ("invalidated", "item", doc, "DELETED"),
    ]
