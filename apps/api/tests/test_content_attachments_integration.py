"""INPROOM-166 (story INPROOM-28 AC 3, 7): attachments of in-scope Confluence pages become parsed items linked
to their page; they follow the page's scope; downloads are capped and never steered off the allowlist."""

import pytest

from app.config import get_settings
from app.content.references import resolve_attachments
from app.db import tenant_session
from app.db_schema import source_objects
from app.sync import worker
from tests import parsing_fixtures as fx
from tests import test_confluence_sources_integration as conf

pytestmark = pytest.mark.integration
env = conf.env
selected = conf.selected
SPEC = fx.pdf(["Design spec\nTwo services"], title="Design")
MEDIA = "https://api.media.atlassian.com/file/{}/binary?token=t"


def att(e, aid, path=""):
    return e.client.get(f"/v1/projects/{e.pid}/content/confluence/{e.sid}/att:{aid}{path}", headers=e.h())


def downloads(e):
    return sum(1 for _, p in e.conf.calls if p.endswith("/download"))


def attachments_result(e):
    return e.q(
        "SELECT last_sync->'content'->'attachments' FROM project_confluence_sources WHERE tenant_id=:t"
    )


def test_supported_attachments_are_stored_linked_parsed_and_not_downloaded_twice(selected):
    e = selected
    e.conf.add_attachment(101, "att5001", "design.pdf", SPEC, "application/pdf")
    e.conf.add_attachment(
        101,
        "att5002",
        "notes.md",
        "# Notes\n\nÜnïcode ✓".encode(),
        "text/markdown",
        redirect=MEDIA.format("att5002"),
    )
    assert e.sync().status_code == 200
    pdf = att(e, "att5001").json()
    assert (pdf["status"], pdf["title"], pdf["current"]["media_type"]) == (
        "CURRENT",
        "design.pdf",
        "application/pdf",
    )
    assert pdf["acl"]["page_id"] == "101" and pdf["current"]["provider_version"] == "1"
    assert att(e, "att5001", "/revisions/1/raw").content == SPEC
    assert att(e, "att5002", "/revisions/1/raw").text == "# Notes\n\nÜnïcode ✓"  # via the media redirect
    assert all("authorization" not in r["headers"] for r in e.conf.media_requests)  # no credential leaves
    with tenant_session(e.tenant) as conn:
        parent = (
            conn.execute(source_objects.select().where(source_objects.c.external_id == "att:att5001"))
            .mappings()
            .one()["parent_external_id"]
        )
    assert parent == "101"
    result = attachments_result(e)
    assert (result["stored"], result["skipped"]) == (2, 1)  # diagram.png: not a document type
    assert result["skipped_items"][0]["reason"] == "not a document type INPROOM parses"
    before = downloads(e)
    assert e.sync().status_code == 200
    assert downloads(e) == before and attachments_result(e)["unchanged"] == 2  # same versions: no download
    e.conf.add_attachment(
        101, "att5001", "design.pdf", fx.pdf(["Design spec v2"]), "application/pdf", version=2
    )
    assert e.sync().status_code == 200
    assert att(e, "att5001").json()["revisions"] == 2
    while worker.run_once("w", transports={}, schedule=False, tenant=e.tenant) is not None:
        pass
    derived = att(e, "att5001", "/revisions/2/derivatives").json()["items"]
    assert [(d["producer"], d["status"]) for d in derived] == [("parse", "READY")]


def test_unsuitable_attachments_are_skipped_with_a_reason(selected, monkeypatch):
    e = selected
    e.conf.add_attachment(101, "att6001", "huge.pdf", SPEC, "application/pdf")
    e.conf.add_attachment(101, "att6002", "fake.pdf", b"MZ\x90\x00 an executable", "application/pdf")
    e.conf.add_attachment(
        101,
        "att6003",
        "elsewhere.md",
        b"# x",
        "text/markdown",
        redirect="https://evil.example/file/att6003/binary",
    )
    monkeypatch.setattr(get_settings(), "content_max_bytes", len(SPEC) - 1)
    assert e.sync().status_code == 200
    reasons = {i["attachment_id"]: i["reason"] for i in attachments_result(e)["skipped_items"]}
    assert reasons["att6001"].startswith("larger than the content size limit")
    assert reasons["att6002"] == "content is files of type .pdf"  # the bytes are not a PDF
    assert reasons["att6003"].startswith("download failed")  # redirect off the allowlist
    assert not any(r["host"] == "evil.example" for r in e.conf.media_requests)
    for aid in ("att6001", "att6002", "att6003"):
        assert att(e, aid).status_code == 404  # never stored


def test_attachments_follow_their_page_and_removed_ones_are_deleted(selected):
    e = selected
    e.conf.add_attachment(102, "att7001", "runbook.txt", b"Restart the api.", "text/plain")
    e.conf.add_attachment(102, "att7002", "old.txt", b"Old notes.", "text/plain")
    assert e.sync().status_code == 200
    e.conf.pages[102].groups = ["bots"]  # the page becomes restricted (not included)
    assert e.sync().status_code == 200
    item = att(e, "att7001").json()
    assert item["status"] == "RESTRICTED"
    raw = att(e, "att7001", "/revisions/1/raw")
    assert raw.status_code == 409 and raw.json()["reason"] == "RESTRICTED"
    e.conf.pages[102].groups = []
    e.conf.pages[102].attachments = [a for a in e.conf.pages[102].attachments if a["id"] != "att7002"]
    assert e.sync().status_code == 200
    assert att(e, "att7001", "/revisions/1/raw").text == "Restart the api."  # back with its page
    assert att(e, "att7002").json()["status"] == "DELETED"


def test_page_references_resolve_to_attachment_items(selected):
    e = selected
    e.conf.add_attachment(101, "att8001", "flow.pdf", SPEC, "application/pdf")
    assert e.sync().status_code == 200
    with tenant_session(e.tenant) as conn:
        page = dict(
            conn.execute(source_objects.select().where(source_objects.c.external_id == "101"))
            .mappings()
            .one()
        )
        refs = resolve_attachments(
            conn,
            page,
            [{"filename": "flow.pdf", "section": "a"}, {"filename": "diagram.png", "section": "a"}],
        )
    assert refs[0]["item"]["external_id"] == "att:att8001" and refs[0]["item"]["status"] == "CURRENT"
    assert refs[1]["item"] is None  # an image: not stored
