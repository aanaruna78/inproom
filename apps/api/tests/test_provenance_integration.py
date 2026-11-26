"""INPROOM-182 (story INPROOM-30 AC 1, 2, 4): provenance records - where a cited passage came from - for every
source kind; unknown metadata is explicit; unauthorized citations reveal nothing."""

import pytest

from tests import test_confluence_sources_integration as conf
from tests import test_jira_sources_integration as jira
from tests.sync_helpers import run_sync
from tests.test_document_api_integration import drain
from tests.test_documents_integration import upload

pytestmark = pytest.mark.integration
env, selected = conf.env, conf.selected
jira_env = jira.env


def resolve(e, *citations, headers=None):
    r = e.client.post(
        f"/v1/projects/{e.pid}/provenance/resolve",
        json={"citations": list(citations)},
        headers=headers or e.h(),
    )
    assert r.status_code == 200, r.text
    return r.json()


def cite(kind, source_id, external_id, seq=1, anchor=None):
    return {"kind": kind, "source_id": str(source_id), "external_id": external_id, "seq": seq} | (
        {"anchor": anchor} if anchor else {}
    )


def test_a_confluence_page_and_its_section(selected):
    e = selected
    drain(e)
    [page] = resolve(e, cite("confluence", e.sid, "101"))["results"]
    rec = page["record"]
    assert (rec["source"]["kind"], rec["source"]["name"], rec["source"]["title"]) == (
        "confluence",
        "ENG",
        "Architecture",
    )
    assert rec["source"]["url"] == "https://acme.atlassian.net/wiki/spaces/ENG/pages/101"
    assert (rec["revision"]["seq"], rec["revision"]["provider_version"]) == (1, "3")
    assert rec["revision"]["retrieved_at"] and rec["revision"]["content_stored"] is True
    assert rec["modified"] == {"at": "2026-11-13T09:00:00.000Z", "by": "Ada"}  # version 3 by Ada
    assert rec["derivation"]["method"] == "verbatim" and rec["freshness"]["status"] == "current"
    assert rec["access"]["fetched_under"]["space_key"] == "ENG" and rec["access"]["item_status"] == "CURRENT"
    assert (rec["trust"]["tier"], rec["trust"]["classification"], rec["trust"]["origin"]) == (
        "trusted",
        "internal",
        "default",
    )
    assert rec["completeness"] == {"score": 1.0, "missing": []}
    [section] = resolve(e, cite("confluence", e.sid, "101", anchor="architecture"))["results"]
    passage, derivation = section["record"]["passage"], section["record"]["derivation"]
    assert (passage["kind"], passage["heading"], passage["path"]) == (
        "section",
        "Architecture",
        ["Architecture"],
    )
    assert (derivation["method"], derivation["producer"], derivation["producer_version"]) == (
        "extracted",
        "parse",
        2,
    )


def test_unknown_metadata_is_listed_never_invented(env, selected):
    e = selected
    doc = upload(e, "notes.md", b"# Notes\n\nText").json()["document_id"]
    e.conf.add_attachment(101, "att3001", "spec.txt", b"Spec text", "text/plain")
    assert e.sync().status_code == 200
    up, att = (
        r["record"]
        for r in resolve(e, cite("upload", e.pid, doc), cite("confluence", e.sid, "att:att3001"))["results"]
    )
    assert up["source"]["url"] is None and up["modified"]["by"] == "alice" and up["modified"]["at"]
    assert {m["field"] for m in up["completeness"]["missing"]} == {"source.url", "revision.provider_version"}
    assert att["modified"] == {"at": None, "by": None}
    reasons = {m["field"]: m["reason"] for m in att["completeness"]["missing"]}
    assert reasons["modified.at"].startswith("Confluence does not report attachment")
    assert att["completeness"]["score"] < 1


def test_jira_fields(jira_env):
    e = jira_env
    e.cid = e.connect().json()["id"]
    e.sid = e.select().json()["id"]
    run_sync({"jira": e.jira.transport()}, e.tenant)
    drain(e)
    issue = e.client.get(f"/v1/projects/{e.pid}/content?kind=jira", headers=e.h()).json()["items"][0]
    [r] = resolve(e, cite("jira", e.sid, issue["external_id"], anchor="field-summary"))["results"]
    rec = r["record"]
    assert rec["source"]["url"].startswith("https://acme.atlassian.net/browse/ABC-")
    assert rec["modified"]["at"] == issue["current"]["provider_version"] and rec["modified"]["by"] is None
    assert rec["passage"]["anchor"] == "field-summary" and rec["derivation"]["producer"] == "parse"


def test_a_superseded_revision_says_so(selected):
    e = selected
    e.conf.edit(101, "<h1>Architecture</h1><p>v4</p>")
    assert e.sync().status_code == 200
    [old] = resolve(e, cite("confluence", e.sid, "101", seq=1))["results"]
    fresh = old["record"]["freshness"]
    assert (fresh["status"], fresh["current_seq"]) == ("superseded", 2)
    assert fresh["reasons"][0]["code"] == "NEWER_REVISION"


def test_unauthorized_citations_reveal_nothing_and_do_not_fail_the_batch(selected):
    e = selected
    drain(e)  # parsed: an unknown anchor is then a definite ANCHOR_NOT_FOUND
    e.conf.pages[102].groups = ["bots"]  # becomes restricted (withheld)
    assert e.sync().status_code == 200
    import uuid

    body = resolve(
        e,
        cite("confluence", e.sid, "101"),
        cite("confluence", e.sid, "102"),
        cite("confluence", uuid.uuid4(), "101"),  # not a source of this project
        cite("confluence", e.sid, "101", seq=9),
        cite("confluence", e.sid, "101", anchor="no-such-section"),
    )
    codes = [r["error"]["code"] if r["error"] else "OK" for r in body["results"]]
    assert codes == ["OK", "CONTENT_WITHHELD", "NOT_FOUND", "NOT_FOUND", "ANCHOR_NOT_FOUND"]
    withheld = body["results"][1]
    assert withheld["record"] is None and withheld["error"]["reason"] == "RESTRICTED"
    assert "Runbooks" not in str(withheld)  # no title, URL or hash of the withheld page
    assert body["counts"] == {"resolved": 1, "withheld": 1, "not_found": 2, "anchor_not_found": 1}
    denied = e.q(
        "SELECT reason FROM audit_events WHERE tenant_id=:t "
        "AND action='provenance.resolve' AND outcome='DENIED'"
    )
    assert denied.startswith("3 of 5 citations refused")


def test_the_revision_route_and_input_limits(selected):
    e = selected
    url = f"/v1/projects/{e.pid}/content/confluence/{e.sid}/101/revisions/1/provenance"
    assert e.client.get(url, headers=e.h()).json()["source"]["title"] == "Architecture"
    drain(e)
    assert e.client.get(url, params={"anchor": "nope"}, headers=e.h()).json()["code"] == "ANCHOR_NOT_FOUND"
    too_many = [cite("confluence", e.sid, "101")] * 101
    r = e.client.post(f"/v1/projects/{e.pid}/provenance/resolve", json={"citations": too_many}, headers=e.h())
    assert r.status_code == 422
    bad = e.client.post(
        f"/v1/projects/{e.pid}/provenance/resolve",
        json={"citations": [{"kind": "svn", "source_id": "x", "external_id": "", "seq": 0}]},
        headers=e.h(),
    )
    assert bad.status_code == 422
