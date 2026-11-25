"""INPROOM-167 (story INPROOM-28 AC 2, 6, 8): parsed documents are served with the raw-content rules; parse
failures are listed with actionable codes and emitted as events."""

import pytest

from app.sync import worker
from tests import parsing_fixtures as fx
from tests import test_confluence_sources_integration as conf
from tests.test_documents_integration import member, upload

pytestmark = pytest.mark.integration
env = conf.env
selected = conf.selected
GUIDE = b"# Guide\n\nIntro.\n\n## Setup\n\n| Step | Cmd |\n|---|---|\n| 1 | make up |\n"


def drain(e):
    results = []
    while (out := worker.run_once("w", transports={}, schedule=False, tenant=e.tenant)) is not None:
        results.append(out)
    return results


def document(e, kind, source, external, seq=1, headers=None):
    url = f"/v1/projects/{e.pid}/content/{kind}/{source}/{external}/revisions/{seq}/document"
    return e.client.get(url, headers=headers or e.h())


def failures(e, query=""):
    return e.client.get(f"/v1/projects/{e.pid}/content/parse-failures{query}", headers=e.h())


def test_a_parsed_upload_is_served_with_anchors_and_tables(env):
    e = env
    doc = upload(e, "guide.md", GUIDE).json()["document_id"]
    pending = document(e, "upload", e.pid, doc)
    assert pending.status_code == 409 and pending.json()["code"] == "NOT_PARSED"
    assert pending.json()["parse_status"] == "PENDING"
    drain(e)
    r = document(e, "upload", e.pid, doc)
    assert r.status_code == 200, r.text
    body = r.json()
    assert (body["producer"], body["producer_version"], body["seq"]) == ("parse", 1, 1)
    sections = body["document"]["sections"]
    assert [s["anchor"] for s in sections] == ["guide", "setup"]
    assert sections[1]["path"] == ["Guide", "Setup"] and sections[1]["tables"] == [
        [["Step", "Cmd"], ["1", "make up"]]
    ]
    viewer = member(e, "vic", "viewer")
    assert document(e, "upload", e.pid, doc, headers=viewer).status_code == 200


def test_failed_parses_are_visible_with_their_code_and_recover_when_fixed(env):
    e = env
    scan = upload(e, "scan.pdf", fx.pdf([None, None])).json()["document_id"]
    locked = upload(e, "locked.pdf", fx.pdf(["secret"], password="pw")).json()["document_id"]
    upload(e, "fine.md", GUIDE)
    results = [r for r in drain(e) if r["source_kind"] == "derive"]
    assert results[0]["result"]["failures"] == {"NO_TEXT_LAYER": 1, "PDF_ENCRYPTED": 1}
    r = document(e, "upload", e.pid, scan)
    assert r.status_code == 409 and (r.json()["parse_status"], r.json()["failure"]) == (
        "FAILED",
        "NO_TEXT_LAYER",
    )
    listed = failures(e).json()
    assert {(x["external_id"], x["code"]) for x in listed["items"]} == {
        (scan, "NO_TEXT_LAYER"),
        (locked, "PDF_ENCRYPTED"),
    }
    assert all(x["detail"] and x["title"] for x in listed["items"])
    assert e.q("SELECT count(*) FROM outbox_events WHERE tenant_id=:t AND type='content.parse.failed'") == 2
    first = failures(e, "?limit=1").json()
    rest = failures(e, f"?limit=1&cursor={first['next_cursor']}").json()
    assert len(first["items"]) == len(rest["items"]) == 1 and first["items"][0] != rest["items"][0]
    assert rest["next_cursor"] is None
    assert failures(e, "?cursor=bm9wZQ").json()["code"] == "INVALID_CURSOR"
    # The user uploads a text-based version: the failure is gone from the list.
    url = f"/v1/projects/{e.pid}/documents/{scan}"
    fixed = e.client.put(
        url, files={"file": ("scan.pdf", fx.pdf(["Now with text"]))}, headers=e.h(**{"If-Match": '"1"'})
    )
    assert fixed.status_code == 200
    drain(e)
    assert [x["external_id"] for x in failures(e).json()["items"]] == [locked]
    assert (
        document(e, "upload", e.pid, scan, seq=2).json()["document"]["sections"][0]["text"] == "Now with text"
    )


def test_documents_of_withheld_items_are_withheld(env):
    e = env
    doc = upload(e, "x.md", GUIDE).json()["document_id"]
    drain(e)
    assert e.client.delete(f"/v1/projects/{e.pid}/documents/{doc}", headers=e.h()).status_code == 204
    r = document(e, "upload", e.pid, doc)
    assert r.status_code == 409 and (r.json()["code"], r.json()["reason"]) == ("CONTENT_WITHHELD", "DELETED")


def test_a_page_document_resolves_its_attachment_references(selected):
    e = selected
    e.conf.add_attachment(101, "att9101", "flow.pdf", fx.pdf(["Flow"]), "application/pdf")
    e.conf.edit(
        101,
        "<h1>Architecture</h1><p>See the flow.</p>"
        '<ac:image><ri:attachment ri:filename="flow.pdf"/></ac:image>'
        '<ac:image><ri:attachment ri:filename="diagram.png"/></ac:image>',
    )
    assert e.sync().status_code == 200
    drain(e)
    page = document(e, "confluence", e.sid, "101", seq=2).json()["document"]
    refs = {a["filename"]: a["item"] for a in page["attachments"]}
    assert refs["flow.pdf"]["external_id"] == "att:att9101" and refs["diagram.png"] is None
    assert document(e, "confluence", e.sid, "att:att9101").json()["document"]["format"] == "pdf"
