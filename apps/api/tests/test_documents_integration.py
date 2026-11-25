"""INPROOM-165 (story INPROOM-28 AC 1, 6): uploaded documents are versioned, parsed source items; the type
is decided from the bytes; settings and roles gate every change."""

import pytest

from app.config import get_settings
from app.modules.documents import clean_filename
from app.sync import worker
from tests import parsing_fixtures as fx
from tests import test_confluence_sources_integration as conf

pytestmark = pytest.mark.integration
env = conf.env


def upload(e, name, data, headers=None, **form):
    return e.client.post(
        f"/v1/projects/{e.pid}/documents", files={"file": (name, data)}, data=form, headers=headers or e.h()
    )


def item(e, doc, path=""):
    return e.client.get(f"/v1/projects/{e.pid}/content/upload/{e.pid}/{doc}{path}", headers=e.h())


def drain(e):
    while worker.run_once("w", transports={}, schedule=False, tenant=e.tenant) is not None:
        pass


def member(e, user, role):
    headers = e.h(user, roles=())
    assert e.client.get("/v1/me", headers=headers).status_code == 200
    r = e.client.put(f"/v1/projects/{e.pid}/members/{user}", json={"roles": [role]}, headers=e.h())
    assert r.status_code == 200, r.text
    return headers


def settings(e, **sources):
    current = e.client.get(f"/v1/projects/{e.pid}/settings", headers=e.h()).json()
    body = {"settings": {"sources": current["settings"]["sources"] | sources}}
    headers = e.h(**{"If-Match": f'"{current["version"]}"'})
    assert e.client.put(f"/v1/projects/{e.pid}/settings", json=body, headers=headers).status_code == 200


def test_an_uploaded_pdf_is_stored_versioned_and_parsed(env):
    e = env
    data = fx.pdf(["Requirements\nLogin with SSO", "Out of scope"], title="Spec")
    r = upload(e, "spec.pdf", data, title="Login spec")
    assert r.status_code == 201, r.text
    doc = r.json()
    assert (doc["media_type"], doc["title"], doc["filename"], doc["seq"], doc["created"]) == (
        "application/pdf",
        "Login spec",
        "spec.pdf",
        1,
        True,
    )
    assert item(e, doc["document_id"], "/revisions/1/raw").content == data
    drain(e)
    parsed = [
        d
        for d in item(e, doc["document_id"], "/revisions/1/derivatives").json()["items"]
        if d["producer"] == "parse"
    ]
    assert [p["status"] for p in parsed] == ["READY"]
    assert e.q("SELECT count(*) FROM audit_events WHERE tenant_id=:t AND action='content.upload'") == 1


def test_a_retried_upload_with_the_same_key_creates_one_document(env):
    e = env
    key = {"Idempotency-Key": "upload-once-0001"}
    first = upload(e, "a.md", b"# A\n\ntext", headers=e.h(**key))
    again = upload(e, "a.md", b"# A\n\ntext", headers=e.h(**key))
    assert first.status_code == again.status_code == 201
    assert again.json()["document_id"] == first.json()["document_id"]
    assert again.headers.get("idempotent-replayed") == "true"
    assert e.q("SELECT count(*) FROM source_objects WHERE tenant_id=:t AND source_kind='upload'") == 1


def test_replace_needs_the_current_revision_and_identical_bytes_add_nothing(env):
    e = env
    doc = upload(e, "notes.md", b"# Notes\n\nv1").json()["document_id"]
    url = f"/v1/projects/{e.pid}/documents/{doc}"
    files = {"file": ("notes.md", b"# Notes\n\nv2")}
    assert e.client.put(url, files=files, headers=e.h()).status_code == 428
    assert e.client.put(url, files=files, headers=e.h(**{"If-Match": '"7"'})).status_code == 412
    r = e.client.put(url, files=files, headers=e.h(**{"If-Match": '"1"'}))
    assert r.status_code == 200 and (r.json()["seq"], r.json()["created"]) == (2, True)
    assert r.headers["etag"] == '"2"'
    same = e.client.put(url, files=files, headers=e.h(**{"If-Match": '"2"'}))
    assert (same.json()["seq"], same.json()["created"]) == (2, False)
    lineage = item(e, doc).json()["lineage"]
    assert [(x["seq"], x["parent_seq"]) for x in lineage] == [(1, None), (2, 1)]


def test_delete_withholds_the_content_and_is_idempotent(env):
    e = env
    doc = upload(e, "gone.txt", b"short lived").json()["document_id"]
    url = f"/v1/projects/{e.pid}/documents/{doc}"
    assert e.client.delete(url, headers=e.h()).status_code == 204
    assert e.client.delete(url, headers=e.h()).status_code == 204
    raw = item(e, doc, "/revisions/1/raw")
    assert raw.status_code == 409 and raw.json()["reason"] == "DELETED"
    assert item(e, doc).json()["status"] == "DELETED"  # lineage stays visible
    put = e.client.put(url, files={"file": ("gone.txt", b"x")}, headers=e.h(**{"If-Match": '"1"'}))
    assert put.status_code == 404
    assert e.client.delete(f"/v1/projects/{e.pid}/documents/{e.pid}", headers=e.h()).status_code == 404


@pytest.mark.parametrize(
    ("name", "data", "status", "media_type"),
    [
        ("report.txt", fx.pdf(["really a pdf"]), 201, "application/pdf"),  # the bytes win over the name
        ("design.docx", fx.docx(lambda d: d.add_paragraph("x")), 201, None),
        ("README.md", "# Ünïcode ✓".encode(), 201, "text/markdown"),
        ("page.html", b"<h1>Hi</h1>", 201, "text/html"),
        ("tool.exe", b"MZ\x90\x00binary", 415, None),
        ("archive.docx", fx.zip_bomb(100), 201, None),  # a zip WITH word/document.xml: parse decides later
        ("plain.zip", b"PK\x03\x04junk", 415, None),
        ("data.txt", b"\x00\x01\x02", 415, None),
        ("latin1.md", "caf\xe9".encode("latin-1"), 415, None),
        ("image.png", b"\x89PNG\r\n", 415, None),
        ("empty.txt", b"", 422, None),
    ],
)
def test_the_media_type_comes_from_the_bytes(env, name, data, status, media_type):
    r = upload(env, name, data)
    assert r.status_code == status, r.text
    if media_type:
        assert r.json()["media_type"] == media_type
    if status == 415:
        assert r.json()["code"] == "UNSUPPORTED_MEDIA_TYPE"


def test_size_limit_and_filename_sanitising(env, monkeypatch):
    e = env
    monkeypatch.setattr(get_settings(), "content_max_bytes", 10)
    r = upload(e, "big.txt", b"x" * 11)
    assert r.status_code == 413 and r.json()["code"] == "DOCUMENT_TOO_LARGE"
    r = upload(e, "../../etc/notes.md", b"# ok")  # (clients percent-encode control characters)
    assert r.status_code == 201 and r.json()["filename"] == "notes.md"
    assert clean_filename("..\\..\\x/pass\x00wd\n.md") == "passwd.md"
    assert clean_filename("  ...  ") == "document" and len(clean_filename("é" * 500)) == 200


def test_uploads_follow_the_project_settings(env):
    e = env
    doc = upload(e, "kept.md", b"# Kept").json()["document_id"]
    settings(e, allowed_kinds=["git", "jira", "confluence"])
    r = upload(e, "new.md", b"# New")
    assert r.status_code == 409 and r.json()["code"] == "UPLOADS_NOT_ALLOWED"
    raw = item(e, doc, "/revisions/1/raw")
    assert raw.status_code == 409 and raw.json()["reason"] == "SOURCE_DISABLED"  # withheld while disabled
    settings(e, allowed_kinds=["git", "jira", "confluence", "upload"])
    assert item(e, doc, "/revisions/1/raw").status_code == 200


def test_only_authoring_roles_change_documents(env):
    e = env
    doc = upload(e, "x.md", b"# X").json()["document_id"]
    viewer, developer, qa = (
        member(e, "vic", "viewer"),
        member(e, "dev", "developer"),
        member(e, "qa", "qa_lead"),
    )
    for headers in (viewer, developer):
        assert upload(e, "y.md", b"# Y", headers=headers).status_code == 403
        assert e.client.delete(f"/v1/projects/{e.pid}/documents/{doc}", headers=headers).status_code == 403
    assert upload(e, "z.md", b"# Z", headers=qa).status_code == 201
    assert (
        e.client.get(f"/v1/projects/{e.pid}/content/upload/{e.pid}/{doc}", headers=viewer).status_code == 200
    )
    denied = e.q(
        "SELECT count(*) FROM audit_events WHERE tenant_id=:t AND outcome='DENIED' "
        "AND action='project.content.upload'"
    )
    assert denied == 4
