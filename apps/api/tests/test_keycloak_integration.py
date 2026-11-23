"""AC5: a real access token from the local Keycloak realm is accepted by the API (no JWKS override)."""

import json
import os
import urllib.parse
import urllib.request

import pytest
from fastapi.testclient import TestClient

from app.main import create_app

pytestmark = pytest.mark.integration


@pytest.fixture
def keycloak_url() -> str:
    url = os.environ.get("INPROOM_TEST_KEYCLOAK_URL")
    if not url:
        pytest.skip("INPROOM_TEST_KEYCLOAK_URL not set (local stack only)")
    return url.rstrip("/")


def _password_token(base: str, username: str, password: str) -> str:
    data = urllib.parse.urlencode(
        {
            "grant_type": "password",
            "client_id": "inproom-dev-cli",
            "username": username,
            "password": password,
            "scope": "openid",
        }
    ).encode()
    with urllib.request.urlopen(
        f"{base}/realms/inproom/protocol/openid-connect/token", data, timeout=10
    ) as r:
        return json.loads(r.read())["access_token"]


@pytest.mark.parametrize(
    ("username", "password", "tenant", "role"),
    [
        ("alice", "alice-local", "a0000000-0000-4000-8000-00000000000a", "tenant_admin"),
        ("bob", "bob-local", "b0000000-0000-4000-8000-00000000000b", "tenant_admin"),
        # Project roles in the token are ignored (INPROOM-20): dave is a plain tenant member.
        ("dave", "dave-local", "a0000000-0000-4000-8000-00000000000a", None),
    ],
)
def test_real_keycloak_token_accepted(keycloak_url, monkeypatch, username, password, tenant, role):
    monkeypatch.setenv("INPROOM_OIDC_ISSUER", f"{keycloak_url}/realms/inproom")
    token = _password_token(keycloak_url, username, password)
    r = TestClient(create_app()).get("/v1/me", headers={"Authorization": f"Bearer {token}"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["tenant_id"] == tenant
    assert body["roles"] == ([role] if role else [])
    assert body["email"].startswith(username)
