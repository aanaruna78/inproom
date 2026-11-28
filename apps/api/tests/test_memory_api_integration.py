"""INPROOM-225 (story INPROOM-35 AC 2, 4, 6): snapshots are versioned without duplicates and audited; reading
an old snapshot withholds every line whose item has since become hidden - and shows it again once restored."""

import pytest

from tests import test_confluence_sources_integration as conf
from tests.test_document_api_integration import drain
from tests.test_documents_integration import settings, upload

pytestmark = pytest.mark.integration
env, selected = conf.env, conf.selected


def create(e, reason="test", status=None):
    r = e.client.post(f"/v1/projects/{e.pid}/memory/snapshots", json={"reason": reason}, headers=e.h())
    assert r.status_code in ((status,) if status else (200, 201)), r.text
    return r.json()


def read(e, version, **params):
    r = e.client.get(f"/v1/projects/{e.pid}/memory/snapshots/{version}", params=params, headers=e.h())
    assert r.status_code == 200, r.text
    return r


def test_versions_are_created_once_and_audited(selected):
    e = selected
    upload(e, "adr-1.md", b"# ADR-1 Queues\n\n## Decision\n\nUse a queue.")
    drain(e)
    first = create(e, "first", 201)
    assert (first["version"], first["created"], first["reason"]) == (1, True, "first")
    assert (
        first["counts"]["decisions"] == 1
        and first["feed_head"] > 0
        and first["generator"].startswith("memory@1")
    )
    again = create(e, "retry", 200)  # identical bytes: the same version, nothing new
    assert (again["version"], again["created"], again["sha256"], again["reason"]) == (
        1,
        False,
        first["sha256"],
        "first",
    )
    e.conf.pages[102].add_version("<p>Restart the api, then the worker.</p>")
    assert e.sync().status_code == 200
    drain(e)
    second = create(e, "after edit", 201)
    assert second["version"] == 2 and second["sha256"] != first["sha256"]
    listed = e.client.get(f"/v1/projects/{e.pid}/memory/snapshots", headers=e.h()).json()["items"]
    assert [x["version"] for x in listed] == [2, 1]

    detail = read(e, 1).json()
    assert detail["served_sha256"] == detail["sha256"] and detail["withheld"] == 0
    assert detail["markdown"].startswith("# Project memory: Confluence")
    assert {"kind": "confluence", "external_id": "102", "seq": 1}.items() <= next(
        c for c in detail["citations"] if c["external_id"] == "102"
    ).items()
    md = read(e, 2, format="markdown")
    assert md.headers["content-type"].startswith("text/markdown") and md.headers["x-memory-withheld"] == "0"
    assert "[confluence:102@2]" in md.text and md.headers["etag"] == f'"{second["sha256"]}"'
    assert e.client.get(f"/v1/projects/{e.pid}/memory/snapshots/9", headers=e.h()).json()["code"] == (
        "MEMORY_SNAPSHOT_NOT_FOUND"
    )
    audits = e.q("SELECT count(*) FROM audit_events WHERE tenant_id=:t AND action='memory.snapshot.created'")
    assert audits == 2  # the identical retry created nothing, so audited nothing
    r = e.client.post(f"/v1/projects/{e.pid}/memory/snapshots", json={"reason": ""}, headers=e.h())
    assert r.status_code == 422


def test_an_old_snapshot_never_shows_what_became_hidden(selected):
    e = selected
    upload(e, "adr-1.md", b"# ADR-1 Queues\n\n## Decision\n\nUse a queue.")
    drain(e)
    v = create(e, "baseline", 201)["version"]
    full = read(e, v).json()
    assert "Runbooks" in full["markdown"] and "adr-1.md" in full["markdown"]

    e.conf.pages[102].groups = ["bots"]  # restricted at the source after the snapshot was taken
    assert e.sync().status_code == 200
    drain(e)
    hidden = read(e, v).json()
    assert "Runbooks" not in hidden["markdown"] and hidden["withheld"] >= 1
    assert hidden["served_sha256"] != hidden["sha256"]
    assert all(c["external_id"] != "102" for c in hidden["citations"])

    base = f"/v1/projects/{e.pid}/sources/confluence/{e.sid}"
    assert e.client.post(f"{base}/exclude", headers=e.h()).status_code == 200  # paused
    paused = read(e, v).json()
    assert (
        all(c["kind"] != "confluence" for c in paused["citations"])
        and "Architecture" not in paused["markdown"]
    )
    settings(e, allowed_kinds=["git", "jira", "confluence"])  # uploads disabled
    assert "adr-1.md" not in read(e, v).json()["markdown"]

    settings(e, allowed_kinds=["git", "jira", "confluence", "upload"])
    assert e.client.post(f"{base}/include", headers=e.h()).status_code == 200
    e.conf.pages[102].groups = []
    assert e.sync().status_code == 200
    drain(e)
    restored = read(e, v).json()
    assert restored["withheld"] == 0 and restored["served_sha256"] == restored["sha256"]


def test_telemetry_counts_without_content(selected, caplog):
    """INPROOM-227: one log line per create with sizes and counts - never the document's content."""
    import json

    e = selected
    drain(e)
    with caplog.at_level("INFO", logger="app.modules.memory"):
        create(e, "telemetry", 201)
    [line] = [r.getMessage() for r in caplog.records if r.getMessage().startswith("memory snapshot project=")]
    logged = json.loads(line.split(" ", 3)[3])
    assert logged["version"] == 1 and logged["created"] is True and logged["bytes"] > 0 and logged["ms"] >= 0
    assert logged["counts"]["facts"] >= 1 and logged["redactions"] == {}
    assert "Architecture" not in line and "Runbooks" not in line
