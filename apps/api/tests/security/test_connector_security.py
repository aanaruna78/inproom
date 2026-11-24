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


# ------------------------------------------------ Jira (INPROOM-132; story INPROOM-23 AC 1, 3, 8)


def test_no_jira_token_leaks_anywhere(db_client, tokens, fake_jira, owner_engine, caplog):
    """Drive every Jira flow (good and bad credentials), then search every table row, response and log
    line, including the Basic auth header form (base64 of email:token)."""
    import base64

    from tests.fakes import jira as fj

    caplog.set_level(logging.DEBUG)
    tenant = uuid.uuid4()
    h = bearer(tokens.mint(tenant_id=tenant, user_id="alice", roles=["tenant_admin"]))
    responses = []

    def call(method, path, **kw):
        r = db_client.request(method, path, headers=kw.pop("headers", h), **kw)
        responses.append(r)
        return r

    def connect(name, token, email=fj.EMAIL):
        body = {
            "provider": "jira",
            "display_name": name,
            "base_url": "https://acme.atlassian.net",
            "token": token,
        }
        return call("POST", "/v1/connections", json=body | ({"email": email} if email else {}))

    ws = call("POST", "/v1/workspaces", json={"slug": "jleak", "name": "Leak"}).json()
    pid = call("POST", f"/v1/workspaces/{ws['id']}/projects", json={"key": "JLEAK", "name": "Leak"}).json()[
        "id"
    ]
    connect("bad", fj.BAD_TOKEN)
    connect("wrong email", fj.VALID_TOKEN, email="nobody@acme.example")
    cid = connect("Jira", fj.VALID_TOKEN).json()["id"]
    call("GET", f"/v1/connections/{cid}/jira/projects?project_id={pid}")
    sid = call("PUT", f"/v1/projects/{pid}/sources/jira/{cid}:ABC").json()["id"]
    call("POST", f"/v1/projects/{pid}/sources/jira/{sid}/discover")
    call("GET", f"/v1/projects/{pid}/sources/jira/{sid}/schema")
    call("GET", f"/v1/projects/{pid}/sources/jira/{sid}/issues")
    call("POST", f"/v1/connections/{cid}/check")
    call("PUT", f"/v1/connections/{cid}/credential", json={"token": fj.BAD_TOKEN})
    call("PUT", f"/v1/connections/{cid}/credential", json={"token": fj.VALID_TOKEN})
    call("GET", f"/v1/projects/{pid}/sources/scope")
    call("POST", f"/v1/connections/{cid}/revoke")

    secrets_ = {"VALID_TOKEN": fj.VALID_TOKEN, "BAD_TOKEN": fj.BAD_TOKEN}
    secrets_ |= {
        f"basic({name})": base64.b64encode(f"{fj.EMAIL}:{value}".encode()).decode()
        for name, value in list(secrets_.items())
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
    record("all Jira routes", "Jira token / Basic header leak scan", "none found", "none found")


@pytest.mark.parametrize(
    "site",
    [
        "http://acme.atlassian.net",
        "https://atlassian.net.evil.com",
        "https://acme.atlassian.net@169.254.169.254",
        "https://169.254.169.254",
        "https://localhost",
        "https://acme.atlassian.net:8443",
    ],
)
def test_jira_site_cannot_be_pointed_at_internal_hosts(db_client, tokens, fake_jira, site):
    from tests.fakes import jira as fj

    h = bearer(tokens.mint(tenant_id=uuid.uuid4(), user_id="alice", roles=["tenant_admin"]))
    body = {
        "provider": "jira",
        "display_name": "x",
        "token": fj.VALID_TOKEN,
        "email": fj.EMAIL,
        "base_url": site,
    }
    r = db_client.post("/v1/connections", json=body, headers=h)
    record("POST /v1/connections (jira)", f"SSRF site {site}", "422", str(r.status_code))
    assert r.status_code == 422 and r.json()["code"] == "BASE_URL_NOT_ALLOWED"
    assert fake_jira.calls == []


# ------------------------------------------------ Confluence (INPROOM-136; story INPROOM-24 AC 1, 7, 8)


def test_no_confluence_token_leaks_anywhere(db_client, tokens, fake_confluence, owner_engine, caplog):
    """Drive every Confluence flow (good and bad credentials, sync, reads, rotate, revoke), then search every
    table row, response and log line, including the Basic auth header form (base64 of email:token)."""
    import base64

    from tests.fakes import confluence as fc

    caplog.set_level(logging.DEBUG)
    tenant = uuid.uuid4()
    h = bearer(tokens.mint(tenant_id=tenant, user_id="alice", roles=["tenant_admin"]))
    responses = []

    def call(method, path, **kw):
        r = db_client.request(method, path, headers=kw.pop("headers", h), **kw)
        responses.append(r)
        return r

    def connect(name, token, email=fc.EMAIL):
        body = {"provider": "confluence", "display_name": name, "base_url": "https://acme.atlassian.net"}
        return call("POST", "/v1/connections", json=body | {"token": token, "email": email})

    ws = call("POST", "/v1/workspaces", json={"slug": "cleak", "name": "Leak"}).json()
    pid = call("POST", f"/v1/workspaces/{ws['id']}/projects", json={"key": "CLEAK", "name": "Leak"}).json()[
        "id"
    ]
    connect("bad", fc.BAD_TOKEN)
    connect("wrong email", fc.VALID_TOKEN, email="nobody@acme.example")
    cid = connect("Confluence", fc.VALID_TOKEN).json()["id"]
    call("GET", f"/v1/connections/{cid}/confluence/spaces?project_id={pid}")
    sid = call(
        "PUT", f"/v1/projects/{pid}/sources/confluence/{cid}:ENG", json={"required_labels": ["approved"]}
    )
    sid = sid.json()["source"]["id"]
    base = f"/v1/projects/{pid}/sources/confluence/{sid}"
    call("POST", f"{base}/sync")
    call("GET", f"{base}/pages")
    call("GET", f"{base}/pages/101")
    call("GET", f"{base}/pages/101/content")
    call("POST", f"/v1/connections/{cid}/check")
    call("PUT", f"/v1/connections/{cid}/credential", json={"token": fc.BAD_TOKEN})
    call("PUT", f"/v1/connections/{cid}/credential", json={"token": fc.VALID_TOKEN})
    call("GET", f"/v1/projects/{pid}/sources/scope")
    call("POST", f"/v1/connections/{cid}/revoke")

    secrets_ = {"VALID_TOKEN": fc.VALID_TOKEN, "BAD_TOKEN": fc.BAD_TOKEN}
    secrets_ |= {
        f"basic({name})": base64.b64encode(f"{fc.EMAIL}:{value}".encode()).decode()
        for name, value in list(secrets_.items())
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
    record("all Confluence routes", "Confluence token / Basic header leak scan", "none found", "none found")


@pytest.mark.parametrize(
    "site",
    [
        "http://acme.atlassian.net",
        "https://atlassian.net.evil.com",
        "https://acme.atlassian.net@169.254.169.254",
        "https://169.254.169.254",
        "https://acme.atlassian.net:8443",
    ],
)
def test_confluence_site_cannot_be_pointed_at_internal_hosts(db_client, tokens, fake_confluence, site):
    from tests.fakes import confluence as fc

    h = bearer(tokens.mint(tenant_id=uuid.uuid4(), user_id="alice", roles=["tenant_admin"]))
    body = {"provider": "confluence", "display_name": "x", "token": fc.VALID_TOKEN, "email": fc.EMAIL}
    r = db_client.post("/v1/connections", json=body | {"base_url": site}, headers=h)
    record("POST /v1/connections (confluence)", f"SSRF site {site}", "422", str(r.status_code))
    assert r.status_code == 422 and r.json()["code"] == "BASE_URL_NOT_ALLOWED"
    assert fake_confluence.calls == []


# ------------------------------------------------ webhook secrets (INPROOM-143; story INPROOM-25 AC 6)


def test_webhook_secret_is_shown_once_and_leaks_nowhere_else(
    db_client, tokens, fake_confluence, owner_engine, caplog
):
    """The generation response is the only place a Jira/Confluence webhook secret appears in plaintext."""
    import hashlib
    import hmac

    from tests.fakes import confluence as fc

    caplog.set_level(logging.DEBUG)
    tenant = uuid.uuid4()
    h = bearer(tokens.mint(tenant_id=tenant, user_id="alice", roles=["tenant_admin"]))
    responses = []

    def call(method, path, **kw):
        r = db_client.request(method, path, headers=kw.pop("headers", h), **kw)
        responses.append(r)
        return r

    ws = call("POST", "/v1/workspaces", json={"slug": "hleak", "name": "Leak"}).json()
    pid = call("POST", f"/v1/workspaces/{ws['id']}/projects", json={"key": "HLEAK", "name": "Leak"}).json()[
        "id"
    ]
    body = {"provider": "confluence", "display_name": "c", "base_url": "https://acme.atlassian.net"}
    cid = call("POST", "/v1/connections", json=body | {"token": fc.VALID_TOKEN, "email": fc.EMAIL}).json()[
        "id"
    ]
    call("PUT", f"/v1/projects/{pid}/sources/confluence/{cid}:ENG", json={"required_labels": ["approved"]})
    generated = db_client.post(f"/v1/connections/{cid}/webhook-secret", headers=h)
    secret, path = generated.json()["secret"], generated.json()["path"]
    payload = b'{"event": "page_updated", "page": {"id": 101, "spaceKey": "ENG"}}'
    signature = "sha256=" + hmac.new(secret.encode(), payload, hashlib.sha256).hexdigest()
    call(
        "POST",
        path,
        content=payload,
        headers={"x-hub-signature": signature, "x-atlassian-webhook-identifier": "1"},
    )
    call("POST", path, content=payload, headers={"x-hub-signature": "sha256=" + "0" * 64})
    call("GET", f"/v1/connections/{cid}")
    call("GET", "/v1/connections")
    call("POST", f"/v1/connections/{cid}/check")
    call("POST", f"/v1/connections/{cid}/revoke")
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
            assert (
                secret not in dump and secret.encode().hex() not in dump
            ), f"webhook secret in table {table}"
    for r in responses:
        assert secret not in r.text and secret not in str(r.headers), f"webhook secret in {r.request.url}"
    assert secret not in caplog.text
    record("POST /v1/connections/{id}/webhook-secret", "secret shown once; leak scan", "once", "once")
