"""INPROOM-146 (story INPROOM-26 AC 4, 5, 7): reauthorization resumes only access pauses, disconnect impact,
revoke stops future syncs/webhooks, and adoption by a new connection with per-source verification."""

import pytest

from app.sync import worker
from tests import test_confluence_sources_integration as helpers
from tests.fakes import confluence as fc
from tests.fakes import jira as fj

env = helpers.env
selected = helpers.selected
pytestmark = pytest.mark.integration


@pytest.fixture
def two_spaces(selected):
    e = selected
    e.hr = e.select("HR", {}).json()["source"]["id"]
    return e


def statuses(e):
    return {
        r.space_key: (r.status, r.pause_cause)
        for r in _rows(
            e, "SELECT space_key, status, pause_cause FROM project_confluence_sources WHERE tenant_id=:t"
        )
    }


def _rows(e, sql):
    from sqlalchemy import text

    from app.db import get_engine

    with get_engine().connect() as conn:
        return conn.execute(text(sql), {"t": e.tenant}).all()


def rotate(e, token=fc.VALID_TOKEN, cid=None):
    return e.client.put(f"/v1/connections/{cid or e.cid}/credential", json={"token": token}, headers=e.h())


def test_reauthorize_resumes_only_access_pauses(two_spaces):
    e = two_spaces
    e.conf.revoke_space(fc.VALID_TOKEN, "ENG")
    e.client.post(f"/v1/connections/{e.cid}/check", headers=e.h())
    e.client.post(f"/v1/projects/{e.pid}/sources/confluence/{e.hr}/exclude", headers=e.h())
    assert statuses(e) == {"ENG": ("PAUSED", "access"), "HR": ("PAUSED", "excluded")}
    blocked = rotate(e).json()  # access not restored yet: nothing resumes, and the reply says why
    assert blocked["resumed"] == [] and [b["name"] for b in blocked["still_blocked"]] == ["ENG"]
    assert blocked["still_blocked"][0]["problem"]["title"]
    e.conf.users[fc.VALID_TOKEN].spaces["ENG"] = {"read"}
    fixed = rotate(e)
    assert fixed.status_code == 200 and fixed.json()["status"] == "ACTIVE"
    assert [r["name"] for r in fixed.json()["resumed"]] == ["ENG"] and fixed.json()["still_blocked"] == []
    assert statuses(e) == {
        "ENG": ("ACTIVE", None),
        "HR": ("PAUSED", "excluded"),
    }  # the person's exclusion stays
    assert (
        e.q("SELECT count(*) FROM sync_jobs WHERE tenant_id=:t AND hints->>'after' = 'reauthorization'") == 1
    )
    bad = rotate(e, token=fc.BAD_TOKEN)
    assert bad.status_code == 422 and statuses(e)["ENG"] == ("ACTIVE", None)


def test_impact_then_revoke_stops_jobs_and_webhooks(two_spaces, monkeypatch):
    e = two_spaces
    impact = e.client.get(f"/v1/connections/{e.cid}/impact", headers=e.h()).json()
    assert impact["projects"] == 1 and sorted(s["name"] for s in impact["sources"]) == ["ENG", "HR"]
    assert "Disconnecting stops syncing 2 active source(s) in 1 project(s)" in impact["summary"]
    hook = e.client.post(f"/v1/connections/{e.cid}/webhook-secret", headers=e.h()).json()
    assert (
        e.client.get(f"/v1/connections/{e.cid}/impact", headers=e.h()).json()["webhooks"]["webhook_secret"]
        == 1
    )
    e.client.post(f"/v1/projects/{e.pid}/sources/confluence/{e.sid}/reconcile", headers=e.h())
    e.client.post(f"/v1/connections/{e.cid}/revoke", headers=e.h())
    assert e.q("SELECT count(*) FROM sync_jobs WHERE tenant_id=:t AND status='QUEUED'") == 0
    assert worker.run_once("w", transports={"confluence": e.conf.transport()}, tenant=e.tenant) is None
    assert (
        e.client.post(hook["path"], content=b"{}", headers={"x-hub-signature": "sha256=0"}).status_code == 404
    )
    assert statuses(e) == {"ENG": ("PAUSED", "connection_revoked"), "HR": ("PAUSED", "connection_revoked")}


def test_new_connection_adopts_verified_sources(two_spaces):
    e = two_spaces
    e.client.post(f"/v1/connections/{e.cid}/revoke", headers=e.h())
    attention = e.client.get("/v1/connections/attention", headers=e.h()).json()
    [revoked] = [a for a in attention if a["connection_id"] == e.cid]
    assert revoked["problems"][0]["code"] == "REVOKED" and "adopt" in revoked["problems"][0]["action"]
    # The new connection's account can read ENG but not HR.
    new = e.connect(token=fc.READONLY_TOKEN, email="reader@acme.example", name="Confluence 2").json()["id"]
    result = e.client.post(
        f"/v1/connections/{new}/adopt", json={"from_connection_id": e.cid}, headers=e.h()
    ).json()
    assert [s["name"] for s in result["adopted"]] == ["ENG"]
    assert [(s["name"], s["status_reason"]) for s in result["still_blocked"]] == [
        ("HR", "the new credential cannot read this source")
    ]
    assert statuses(e) == {"ENG": ("ACTIVE", None), "HR": ("PAUSED", "access")}
    assert e.q("SELECT count(*) FROM sync_jobs WHERE tenant_id=:t AND hints->>'after' = 'adoption'") == 1
    again = e.client.post(
        f"/v1/connections/{new}/adopt", json={"from_connection_id": e.cid}, headers=e.h()
    ).json()
    assert (
        again["adopted"] == []
        and e.q(
            "SELECT count(*) FROM audit_events WHERE tenant_id=:t AND action='connection.sources_adopted'"
        )
        == 1
    )
    sources = e.client.get(f"/v1/connections/{new}/sources", headers=e.h()).json()
    assert sorted((s["name"], s["project_key"]) for s in sources) == [("ENG", "CNF"), ("HR", "CNF")]


def test_adopt_guards(two_spaces, fake_jira):
    e = two_spaces
    other = e.connect(name="Confluence 3").json()["id"]
    not_revoked = e.client.post(
        f"/v1/connections/{other}/adopt", json={"from_connection_id": e.cid}, headers=e.h()
    )
    assert not_revoked.status_code == 409 and not_revoked.json()["code"] == "ADOPT_NOT_ALLOWED"
    e.client.post(f"/v1/connections/{e.cid}/revoke", headers=e.h())
    jira = e.client.post(
        "/v1/connections",
        json={
            "provider": "jira",
            "display_name": "J",
            "base_url": "https://acme.atlassian.net",
            "token": fj.VALID_TOKEN,
            "email": fc.EMAIL,
        },
        headers=e.h(),
    ).json()["id"]
    cross = e.client.post(f"/v1/connections/{jira}/adopt", json={"from_connection_id": e.cid}, headers=e.h())
    assert cross.status_code == 409 and cross.json()["code"] == "ADOPT_NOT_ALLOWED"
    viewer = e.client.post(
        f"/v1/connections/{other}/adopt", json={"from_connection_id": e.cid}, headers=e.h("v", ())
    )
    assert viewer.status_code == 403


def test_attention_lists_invalid_connections_in_plain_language(two_spaces):
    e = two_spaces
    assert all(
        a["connection_id"] != e.cid for a in e.client.get("/v1/connections/attention", headers=e.h()).json()
    )
    e.conf.users[fc.VALID_TOKEN].valid = False
    e.client.post(f"/v1/connections/{e.cid}/check", headers=e.h())
    [item] = [
        a
        for a in e.client.get("/v1/connections/attention", headers=e.h()).json()
        if a["connection_id"] == e.cid
    ]
    assert item["status"] == "INVALID" and item["problems"][0]["title"] == "Reauthorization needed"
    assert item["affected_sources"] == 2
    assert e.client.get("/v1/connections/attention", headers=e.h("v", ())).status_code == 403
