"""INPROOM-226 (story INPROOM-35 AC 2, 3, 4): diff between versions, verify (rebuild and compare), and purge
deleting the snapshot lines of purged content."""

from datetime import UTC, datetime, timedelta

import pytest

from app.content import retention, store
from tests import test_confluence_sources_integration as conf
from tests.test_document_api_integration import drain
from tests.test_documents_integration import upload
from tests.test_memory_api_integration import create, read

pytestmark = pytest.mark.integration
env, selected = conf.env, conf.selected


def verify(e, version):
    r = e.client.post(f"/v1/projects/{e.pid}/memory/snapshots/{version}/verify", headers=e.h())
    assert r.status_code == 200, r.text
    return r.json()


def test_diff_and_verify(selected):
    e = selected
    drain(e)
    v1 = create(e, "one", 201)
    assert verify(e, 1) | {"drift": ""} == verify(e, 1) and verify(e, 1)["reproducible"] is True
    e.conf.pages[102].add_version("<p>Restart the api, then the worker.</p>")
    assert e.sync().status_code == 200
    drain(e)
    drifted = verify(e, 1)
    assert drifted["reproducible"] is False and drifted["current_sha256"] != v1["sha256"]
    assert "-- Page: Runbooks [confluence:102@1]" in drifted["drift"]
    assert "+- Page: Runbooks [confluence:102@2]" in drifted["drift"]
    create(e, "two", 201)
    assert verify(e, 2)["reproducible"] is True

    r = e.client.get(f"/v1/projects/{e.pid}/memory/snapshots/2/diff", params={"against": 1}, headers=e.h())
    assert r.status_code == 200, r.text
    d = r.json()
    assert (d["from_version"], d["to_version"], d["added"], d["removed"]) == (1, 2, 1, 1)
    assert d["sections"] == [{"section": "facts", "added": 1, "removed": 1}]
    assert "[confluence:102@2]" in d["unified"]
    r = e.client.get(f"/v1/projects/{e.pid}/memory/snapshots/2/diff", params={"against": 7}, headers=e.h())
    assert r.status_code == 404 and r.json()["code"] == "MEMORY_SNAPSHOT_NOT_FOUND"
    r = e.client.get(f"/v1/projects/{e.pid}/memory/snapshots/2/diff", headers=e.h())
    assert r.status_code == 422  # `against` is required


def test_purge_deletes_the_lines_of_purged_content(selected):
    e = selected
    doc = upload(e, "adr-9.md", b"# ADR-9 Retries\n\n## Decision\n\nRetry three times.").json()["document_id"]
    drain(e)
    create(e, "with the ADR", 201)
    assert "adr-9.md" in read(e, 1).json()["markdown"]
    assert e.client.delete(f"/v1/projects/{e.pid}/documents/{doc}", headers=e.h()).status_code == 204
    hidden = read(e, 1).json()
    assert "adr-9.md" not in hidden["markdown"] and hidden["withheld"] == 1  # deleted: withheld at once
    result = retention.purge_project(
        e.tenant, e.pid, store.get_store(), datetime.now(UTC) + timedelta(days=3660)
    )
    assert result["memory_lines_deleted"] == 1
    after = read(e, 1).json()
    assert (
        "adr-9.md" not in after["markdown"] and after["withheld"] == 0
    )  # gone from the database, not hidden
    assert e.q("SELECT count(*) FROM memory_snapshot_entries WHERE tenant_id=:t AND line LIKE '%adr-9%'") == 0
