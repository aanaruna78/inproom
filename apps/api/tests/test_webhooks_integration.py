"""INPROOM-110 (story INPROOM-22 AC 6, 7): webhook registration and the signed, replay-safe receiver."""

import hashlib
import hmac
import json
import uuid

import pytest
from sqlalchemy import text

from app.config import get_settings
from tests import test_connections_integration as helpers
from tests.auth_helpers import bearer

env = helpers.env
connected = helpers.connected
pytestmark = pytest.mark.integration
PUBLIC = "https://inproom.example.com"


@pytest.fixture
def public_url(monkeypatch):
    monkeypatch.setenv("INPROOM_PUBLIC_URL", PUBLIC)
    get_settings.cache_clear()


def _select(env, repo, branches=None, project=None, register=True):
    return env.client.put(
        f"/v1/projects/{project or env.pid}/sources/repositories/{env.cid}/{repo}",
        json={"branches": branches or ["main"], "register_webhook": register},
        headers=env.h(),
    )


def _deliver(
    env, payload, event="push", delivery=None, secret=None, tenant=None, connection=None, signature=None
):
    body = json.dumps(payload).encode()
    secret = secret if secret is not None else env.gh.hooks[1][0]["secret"]
    sig = signature or "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    return env.client.post(
        f"/v1/hooks/github/{tenant or env.tenant}/{connection or env.cid}",
        content=body,
        headers={
            "content-type": "application/json",
            "x-github-event": event,
            "x-github-delivery": delivery or str(uuid.uuid4()),
            "x-hub-signature-256": sig,
        },
    )


def _push(branch="main", repo=1):
    return {
        "ref": f"refs/heads/{branch}",
        "after": "f" * 40,
        "repository": {"id": repo, "full_name": "acme/api"},
    }


def _changed(env):
    return env.q("SELECT count(*) FROM outbox_events WHERE tenant_id=:t AND type='source.repository.changed'")


# ---------------------------------------------------------------- registration


def test_without_public_url_webhook_is_not_configured(connected):
    env = connected
    r = _select(env, "1")
    assert r.status_code == 200 and r.json()["webhook_status"] == "not_configured"
    assert env.gh.hooks.get(1, []) == []


def test_registration_is_idempotent_and_shared_across_projects(connected, public_url):
    env = connected
    assert _select(env, "1").json()["webhook_status"] == "registered"
    _select(env, "1")
    _select(env, "1", ["main", "release/1.0"])
    hooks = env.gh.hooks[1]
    assert len(hooks) == 1
    assert hooks[0]["url"] == f"{PUBLIC}/v1/hooks/github/{env.tenant}/{env.cid}"
    assert hooks[0]["events"] == ["push", "create", "delete", "repository"]
    ws = env.client.get("/v1/workspaces", headers=env.h()).json()["items"][0]
    other = env.client.post(
        f"/v1/workspaces/{ws['id']}/projects", json={"key": "SRC2", "name": "Second"}, headers=env.h()
    ).json()["id"]
    assert _select(env, "1", project=other).json()["webhook_status"] == "registered"
    assert len(env.gh.hooks[1]) == 1  # found and reused, not duplicated
    # removing it from one project keeps the hook the other project still uses...
    assert (
        env.client.delete(
            f"/v1/projects/{env.pid}/sources/repositories/{env.cid}/1", headers=env.h()
        ).status_code
        == 204
    )
    assert len(env.gh.hooks[1]) == 1
    # ...removing the last use deletes it
    assert (
        env.client.delete(
            f"/v1/projects/{other}/sources/repositories/{env.cid}/1", headers=env.h()
        ).status_code
        == 204
    )
    assert env.gh.hooks[1] == []


def test_missing_webhook_permission_keeps_selection(connected, public_url):
    env = connected
    r = _select(env, "2")  # token can read acme/web but has no Webhooks permission
    assert r.status_code == 200 and r.json()["status"] == "ACTIVE" and r.json()["webhook_status"] == "failed"


def test_webhook_secret_is_only_stored_encrypted(connected, public_url, owner_engine):
    env = connected
    _select(env, "1")
    secret = env.gh.hooks[1][0]["secret"]
    with owner_engine.connect() as conn:
        dump = conn.execute(
            text(
                "SELECT string_agg(t::text, ' ') FROM (SELECT * FROM connection_secrets WHERE tenant_id=:t) t"
            ),
            {"t": env.tenant},
        ).scalar()
    assert "webhook" in dump and secret not in dump


# ---------------------------------------------------------------- receiver


def test_selected_push_is_processed_once_even_when_replayed(connected, public_url):
    env = connected
    _select(env, "1")
    first = _deliver(env, _push(), delivery="d-1")
    assert first.status_code == 202 and first.json() == {
        "status": "processed",
        "delivery_id": "d-1",
        "projects": 1,
    }
    replay = _deliver(env, _push(), delivery="d-1")
    assert replay.status_code == 200 and replay.json()["status"] == "duplicate"
    assert _changed(env) == 1
    assert env.q("SELECT count(*) FROM webhook_deliveries WHERE tenant_id=:t") == 1
    payload = env.q(
        "SELECT payload::text FROM outbox_events WHERE tenant_id=:t AND type='source.repository.changed'"
    )
    assert '"branch": "main"' in payload and '"after_sha": "' + "f" * 40 in payload


@pytest.mark.parametrize(
    ("payload", "event"),
    [
        (_push("develop"), "push"),  # branch not selected
        (_push("main", repo=2), "push"),  # repository not selected
        ({"zen": "Keep it logically awesome."}, "ping"),
    ],
)
def test_unselected_events_are_acknowledged_and_ignored(connected, public_url, payload, event):
    env = connected
    _select(env, "1")
    r = _deliver(env, payload, event=event)
    assert r.status_code == 202 and r.json()["status"] == "ignored"
    assert _changed(env) == 0


def test_bad_or_missing_signature_is_rejected_and_audited(connected, public_url):
    env = connected
    _select(env, "1")
    for kwargs in ({"secret": "wrong-secret"}, {"signature": "sha256=" + "0" * 64}, {"signature": "garbage"}):
        r = _deliver(env, _push(), **kwargs)
        assert r.status_code == 401 and r.json()["code"] == "WEBHOOK_SIGNATURE_INVALID"
    assert _changed(env) == 0 and env.q("SELECT count(*) FROM webhook_deliveries WHERE tenant_id=:t") == 0
    denied = env.q(
        "SELECT count(*) FROM audit_events "
        "WHERE tenant_id=:t AND action='webhook.receive' AND outcome='DENIED'"
    )
    assert denied == 3


def test_unknown_foreign_and_revoked_endpoints_look_the_same(connected, public_url):
    env = connected
    _select(env, "1")
    secret = env.gh.hooks[1][0]["secret"]
    unknown = _deliver(env, _push(), secret=secret, connection=uuid.uuid4())
    foreign = _deliver(env, _push(), secret=secret, tenant=uuid.uuid4())
    env.client.post(f"/v1/connections/{env.cid}/revoke", headers=env.h())
    revoked = _deliver(env, _push(), secret=secret)

    def shape(r):
        return r.status_code, {k: v for k, v in r.json().items() if k != "trace_id"}

    assert (
        shape(unknown)
        == shape(foreign)
        == shape(revoked)
        == (
            404,
            {
                "type": shape(unknown)[1]["type"],
                "title": "Not found",
                "status": 404,
                "code": "NOT_FOUND",
                "detail": "Webhook endpoint not found",
            },
        )
    )
    assert _changed(env) == 0


def test_oversized_and_malformed_deliveries(connected, public_url):
    env = connected
    _select(env, "1")
    big = env.client.post(
        f"/v1/hooks/github/{env.tenant}/{env.cid}",
        content=b"x" * 1_000_001,
        headers={"x-github-event": "push"},
    )
    assert big.status_code == 413
    no_id = env.client.post(
        f"/v1/hooks/github/{env.tenant}/{env.cid}",
        content=b"{}",
        headers={
            "x-github-event": "push",
            "x-hub-signature-256": "sha256="
            + hmac.new(env.gh.hooks[1][0]["secret"].encode(), b"{}", hashlib.sha256).hexdigest(),
        },
    )
    assert no_id.status_code == 400 and no_id.json()["code"] == "WEBHOOK_HEADERS_MISSING"


def test_receiver_needs_no_user_token_but_management_routes_do(connected, public_url, tokens):
    env = connected
    _select(env, "1")
    assert _deliver(env, _push()).status_code == 202  # no Authorization header at all
    other = bearer(tokens.mint(tenant_id=uuid.uuid4(), user_id="eve", roles=["tenant_admin"]))
    assert env.client.get(f"/v1/connections/{env.cid}", headers=other).status_code == 404
