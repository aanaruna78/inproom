"""INPROOM-183 (story INPROOM-30 AC 3, 7): source trust and classification policies; the project's
classification cap is enforced wherever content is served, and lifting it restores access at once."""

import pytest

from app.content import derivatives
from app.db import tenant_session
from tests import test_confluence_sources_integration as conf
from tests.test_document_api_integration import drain
from tests.test_documents_integration import member, settings, upload
from tests.test_provenance_integration import cite, resolve

pytestmark = pytest.mark.integration
env, selected = conf.env, conf.selected


def policy_url(e, kind="confluence", source=None):
    return f"/v1/projects/{e.pid}/sources/{kind}/{source or e.sid}/policy"


def put(e, version, headers=None, **body):
    data = {"tier": "trusted", "classification": "internal", "reason": "reviewed by the team"} | body
    return e.client.put(policy_url(e), json=data, headers=(headers or e.h()) | {"If-Match": f'"{version}"'})


def raw(e, page="101"):
    return e.client.get(
        f"/v1/projects/{e.pid}/content/confluence/{e.sid}/{page}/revisions/1/raw", headers=e.h()
    )


def test_defaults_then_versioned_audited_policies(selected):
    e = selected
    r = e.client.get(policy_url(e), headers=e.h())
    assert r.status_code == 200 and r.headers["etag"] == '"0"'
    assert {k: r.json()[k] for k in ("tier", "classification", "origin", "version")} == {
        "tier": "trusted",
        "classification": "internal",
        "origin": "default",
        "version": 0,
    }
    no_match = e.client.put(
        policy_url(e), json={"tier": "trusted", "classification": "internal", "reason": "x x"}, headers=e.h()
    )
    assert no_match.status_code == 428
    assert put(e, 7).status_code == 412
    first = put(e, 0, tier="authoritative", reason="the source of record")
    assert first.status_code == 200 and (first.json()["version"], first.json()["origin"]) == (1, "policy")
    again = put(e, 1, tier="authoritative", reason="the source of record")
    assert again.json()["version"] == 1  # identical update: no new version, no new event
    assert e.q("SELECT count(*) FROM audit_events WHERE tenant_id=:t AND action='source.policy.update'") == 1
    assert e.q("SELECT count(*) FROM outbox_events WHERE tenant_id=:t AND type='source.policy.changed'") == 1
    [rec] = resolve(e, cite("confluence", e.sid, "101"))["results"]
    assert (rec["record"]["trust"]["tier"], rec["record"]["trust"]["origin"]) == ("authoritative", "policy")
    assert put(e, 1, tier="sovereign").status_code == 422 and put(e, 1, reason="").status_code == 422
    missing = e.client.get(f"/v1/projects/{e.pid}/sources/confluence/{e.pid}/policy", headers=e.h())
    assert missing.status_code == 404 and missing.json()["code"] == "SOURCE_NOT_FOUND"
    viewer = member(e, "vic", "viewer")
    assert e.client.get(policy_url(e), headers=viewer).status_code == 200
    assert put(e, 1, headers=viewer, tier="untrusted").status_code == 403


def test_content_above_the_project_cap_is_withheld_everywhere_and_restored(selected):
    e = selected
    doc = upload(e, "notes.md", b"# Notes\n\nText").json()["document_id"]
    drain(e)

    def live_items():
        with tenant_session(e.tenant) as conn:
            return {(x["source_kind"], x["external_id"]) for x in derivatives.live(conn, e.pid)}

    assert ("confluence", "101") in live_items()
    assert put(e, 0, classification="confidential", reason="contains salary data").status_code == 200
    r = raw(e)
    assert r.status_code == 409 and r.json()["reason"] == "CLASSIFICATION_EXCEEDS_PROJECT"
    doc_r = e.client.get(
        f"/v1/projects/{e.pid}/content/confluence/{e.sid}/101/revisions/1/document", headers=e.h()
    )
    assert doc_r.status_code == 409 and doc_r.json()["reason"] == "CLASSIFICATION_EXCEEDS_PROJECT"
    [p] = resolve(e, cite("confluence", e.sid, "101"))["results"]
    assert p["record"] is None and p["error"]["reason"] == "CLASSIFICATION_EXCEEDS_PROJECT"
    assert not {i for i in live_items() if i[0] == "confluence"}  # consumers never see it
    assert ("upload", doc) in live_items()  # other sources are unaffected
    settings(e, max_classification="confidential")  # the project now allows it
    assert raw(e).status_code == 200 and ("confluence", "101") in live_items()
    settings(e, max_classification="public")  # defaults (internal) are now above the cap too
    assert (
        e.client.get(
            f"/v1/projects/{e.pid}/content/upload/{e.pid}/{doc}/revisions/1/raw", headers=e.h()
        ).json()["reason"]
        == "CLASSIFICATION_EXCEEDS_PROJECT"
    )
    cap = e.client.get(policy_url(e), headers=e.h()).json()
    assert (cap["project_max_classification"], cap["within_project_cap"]) == ("public", False)
