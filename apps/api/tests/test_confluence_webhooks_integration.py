"""INPROOM-143 (story INPROOM-25 AC 1, 6): signed Atlassian webhooks (shown through Confluence) - secret shown
once, verify, dedup, coalesce into one job, identical 404s, size cap."""

import hashlib
import hmac
import json
import uuid

import pytest

from app.config import get_settings
from app.sync import worker
from tests import test_confluence_sources_integration as helpers
from tests.fakes.github import VALID_TOKEN
from tests.sync_helpers import due

env = helpers.env
selected = helpers.selected
pytestmark = pytest.mark.integration


@pytest.fixture
def hooked(selected, monkeypatch):
    monkeypatch.setenv("INPROOM_PUBLIC_URL", "https://inproom.example.com")
    get_settings.cache_clear()
    e = selected
    r = e.client.post(f"/v1/connections/{e.cid}/webhook-secret", headers=e.h())
    assert r.status_code == 200, r.text
    assert r.headers["cache-control"] == "no-store"
    e.secret, e.path = r.json()["secret"], r.json()["path"]
    assert r.json()["url"] == f"https://inproom.example.com{e.path}"
    return e


def deliver(e, payload, delivery=None, secret=None, path=None, signature=None):
    body = json.dumps(payload).encode()
    sig = signature or "sha256=" + hmac.new((secret or e.secret).encode(), body, hashlib.sha256).hexdigest()
    headers = {"content-type": "application/json", "x-hub-signature": sig}
    if delivery:
        headers["x-atlassian-webhook-identifier"] = delivery
    return e.client.post(path or e.path, content=body, headers=headers)


def page_event(page=101, space="ENG"):
    return {"event": "page_updated", "page": {"id": page, "spaceKey": space}}


def test_deliveries_dedup_coalesce_and_emit_each_change_once(hooked):
    e = hooked
    worker.run_once("w", transports={"confluence": e.conf.transport()}, tenant=e.tenant)  # baseline
    e.conf.edit(101, "<p>webhook-announced edit</p>")
    first = deliver(e, page_event(), delivery="c-1")
    assert first.status_code == 202 and first.json()["projects"] == 1
    assert deliver(e, page_event(), delivery="c-1").json()["status"] == "duplicate"
    for n in range(2, 6):  # four more distinct deliveries while the job is still queued
        assert deliver(e, page_event(), delivery=f"c-{n}").status_code == 202
    assert e.q("SELECT count(*) FROM sync_jobs WHERE tenant_id=:t AND status='QUEUED'") == 1
    assert e.q("SELECT triggers FROM sync_jobs WHERE tenant_id=:t AND status='QUEUED'") == 5
    no_id = deliver(e, page_event(page=102))  # no identifier header: the signed body identifies it
    assert no_id.status_code == 202 and no_id.json()["delivery_id"].startswith("sha256:")
    assert deliver(e, page_event(page=102)).json()["status"] == "duplicate"
    due(e.tenant)
    done = worker.run_once(
        "w", transports={"confluence": e.conf.transport()}, schedule=False, tenant=e.tenant
    )
    assert done["reason"] == "webhook" and done["result"]["counts"]["updated"] == 1
    assert e.events("source.confluence.page.changed") == 4  # 3 at selection + the edit, once
    other_space = deliver(e, page_event(space="HR"), delivery="c-9")
    assert other_space.json()["status"] == "ignored"


def test_bad_signatures_unknown_endpoints_and_limits(hooked):
    e = hooked
    for kw in ({"secret": "wrong"}, {"signature": "sha256=" + "0" * 64}, {"signature": "nonsense"}):
        r = deliver(e, page_event(), delivery="x", **kw)
        assert r.status_code == 401 and r.json()["code"] == "WEBHOOK_SIGNATURE_INVALID"
    assert e.q("SELECT count(*) FROM audit_events WHERE tenant_id=:t AND action='webhook.receive'") == 3
    tenant, cid = e.path.split("/")[-2:]

    def shape(r):
        return r.status_code, {k: v for k, v in r.json().items() if k != "trace_id"}

    unknown = deliver(e, page_event(), path=f"/v1/hooks/confluence/{tenant}/{uuid.uuid4()}")
    foreign = deliver(e, page_event(), path=f"/v1/hooks/confluence/{uuid.uuid4()}/{cid}")
    wrong_provider = deliver(e, page_event(), path=f"/v1/hooks/jira/{tenant}/{cid}")
    e.client.post(f"/v1/connections/{e.cid}/revoke", headers=e.h())
    revoked = deliver(e, page_event())
    assert shape(unknown) == shape(foreign) == shape(wrong_provider) == shape(revoked)
    assert shape(unknown)[0] == 404
    big = e.client.post(e.path, content=b"x" * 1_000_001, headers={"x-hub-signature": "sha256=0"})
    assert big.status_code == 413
    assert e.q("SELECT count(*) FROM sync_jobs WHERE tenant_id=:t") == 0


def test_secret_rotation_and_applicability(hooked, fake_github):
    e = hooked
    old = e.secret
    rotated = e.client.post(f"/v1/connections/{e.cid}/webhook-secret", headers=e.h()).json()["secret"]
    assert rotated != old
    assert deliver(e, page_event(), delivery="old", secret=old).status_code == 401
    assert deliver(e, page_event(), delivery="new", secret=rotated).status_code == 202
    audit = e.q(
        "SELECT string_agg(row_to_json(a)::text, ' ') FROM audit_events a "
        "WHERE tenant_id=:t AND action='connection.webhook_secret.rotated'"
    )
    assert audit and rotated not in audit and old not in audit
    gh = e.client.post("/v1/connections", json={"display_name": "gh", "token": VALID_TOKEN}, headers=e.h())
    r = e.client.post(f"/v1/connections/{gh.json()['id']}/webhook-secret", headers=e.h())
    assert r.status_code == 409 and r.json()["code"] == "WEBHOOK_SECRET_NOT_APPLICABLE"
    viewer = e.client.post(f"/v1/connections/{e.cid}/webhook-secret", headers=e.h(user="v", roles=()))
    assert viewer.status_code == 403
