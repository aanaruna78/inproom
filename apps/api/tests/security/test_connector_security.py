"""INPROOM-110 / story INPROOM-22 AC 1, 8: no credential leaks anywhere, SSRF-safe connection setup."""

import hashlib
import hmac
import json
import logging
import uuid

import pytest
from sqlalchemy import text

from app.config import get_settings
from app.db_schema import TENANT_SCOPED_TABLES
from tests.auth_helpers import bearer
from tests.fakes.github import BAD_TOKEN, OTHER_TOKEN, VALID_TOKEN
from tests.security.conftest import record

pytestmark = pytest.mark.integration


def test_no_credential_or_webhook_secret_leaks_anywhere(
    db_client, tokens, fake_github, owner_engine, monkeypatch, caplog
):
    """Drive every connector flow, then search every table row, every response and every log line."""
    caplog.set_level(logging.DEBUG)
    monkeypatch.setenv("INPROOM_PUBLIC_URL", "https://inproom.example.com")
    get_settings.cache_clear()
    tenant = uuid.uuid4()
    h = bearer(tokens.mint(tenant_id=tenant, user_id="alice", roles=["tenant_admin"]))
    responses = []

    def call(method, path, **kw):
        r = db_client.request(method, path, headers=kw.pop("headers", h), **kw)
        responses.append(r)
        return r

    ws = call("POST", "/v1/workspaces", json={"slug": "leak", "name": "Leak"}).json()
    pid = call("POST", f"/v1/workspaces/{ws['id']}/projects", json={"key": "LEAK", "name": "Leak"}).json()[
        "id"
    ]
    call("POST", "/v1/connections", json={"display_name": "bad", "token": BAD_TOKEN})
    call("POST", "/v1/connections", json={"display_name": "short", "token": "ghp_short_secret_x"})
    cid = call("POST", "/v1/connections", json={"display_name": "GitHub", "token": VALID_TOKEN}).json()["id"]
    call("GET", f"/v1/connections/{cid}/repositories?project_id={pid}")
    call(
        "PUT",
        f"/v1/projects/{pid}/sources/repositories/{cid}/1",
        json={"branches": ["main"], "register_webhook": True},
    )
    webhook_secret = fake_github.hooks[1][0]["secret"]
    body = json.dumps({"ref": "refs/heads/main", "after": "1" * 40, "repository": {"id": 1}}).encode()
    sig = "sha256=" + hmac.new(webhook_secret.encode(), body, hashlib.sha256).hexdigest()
    hook = {"x-github-event": "push", "x-github-delivery": "leak-1", "x-hub-signature-256": sig}
    call("POST", f"/v1/hooks/github/{tenant}/{cid}", content=body, headers=hook)
    call("POST", f"/v1/connections/{cid}/check")
    call("PUT", f"/v1/connections/{cid}/credential", json={"token": BAD_TOKEN})
    call("PUT", f"/v1/connections/{cid}/credential", json={"token": OTHER_TOKEN})
    call("GET", "/v1/connections")
    call("GET", f"/v1/projects/{pid}/sources/scope")
    call("POST", f"/v1/connections/{cid}/revoke")

    secrets_ = {
        "VALID_TOKEN": VALID_TOKEN,
        "OTHER_TOKEN": OTHER_TOKEN,
        "BAD_TOKEN": BAD_TOKEN,
        "webhook secret": webhook_secret,
        "short token": "ghp_short_secret_x",
    }
    with owner_engine.connect() as conn:
        for table in TENANT_SCOPED_TABLES:
            column = "id" if table == "tenants" else "tenant_id"
            dump = conn.execute(
                text(
                    "SELECT coalesce(string_agg(t::text, ' '), '') "
                    f"FROM (SELECT * FROM {table} WHERE {column}=:t) t"
                ),
                {"t": tenant},
            ).scalar()
            for name, value in secrets_.items():
                assert value not in dump, f"{name} found in table {table}"
                assert value.encode().hex() not in dump, f"{name} (hex) found in table {table}"
    for r in responses:
        for name, value in secrets_.items():
            assert value not in r.text and value not in str(r.headers), f"{name} in response {r.request.url}"
    for name, value in secrets_.items():
        assert value not in caplog.text, f"{name} in logs"
    record("all connector routes", "credential/webhook secret leak scan", "none found", "none found")


@pytest.mark.parametrize(
    "base_url",
    [
        "http://api.github.com",
        "https://169.254.169.254",
        "https://api.github.com@169.254.169.254",
        "https://metadata.google.internal",
        "https://localhost",
        "https://api.github.com:8443",
    ],
)
def test_connection_setup_cannot_be_pointed_at_internal_hosts(db_client, tokens, fake_github, base_url):
    h = bearer(tokens.mint(tenant_id=uuid.uuid4(), user_id="alice", roles=["tenant_admin"]))
    r = db_client.post(
        "/v1/connections", json={"display_name": "x", "token": VALID_TOKEN, "base_url": base_url}, headers=h
    )
    record("POST /v1/connections", f"SSRF base_url {base_url}", "422", str(r.status_code))
    assert r.status_code == 422 and r.json()["code"] == "BASE_URL_NOT_ALLOWED"
    assert fake_github.calls == []
