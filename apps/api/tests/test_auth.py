"""AC4: token verification. /v1/me answers from the verified principal even when the DB is unreachable."""

import time
import uuid

import pytest
from cryptography.hazmat.primitives.asymmetric import rsa

from tests.auth_helpers import bearer
from tests.conftest import UNREACHABLE_DB

TENANT = uuid.UUID("a0000000-0000-4000-8000-00000000000a")


@pytest.fixture
def client(client_for):
    return client_for(UNREACHABLE_DB)


def _assert_401(response, code):
    assert response.status_code == 401, response.text
    assert response.headers["content-type"] == "application/problem+json"
    assert response.headers["WWW-Authenticate"].startswith("Bearer")
    assert response.json()["code"] == code


def test_valid_token_yields_principal_from_claims(client, tokens):
    token = tokens.mint(tenant_id=TENANT, user_id="alice", roles=["tenant_admin"], email="a@x.test")
    r = client.get("/v1/me", headers=bearer(token))
    assert r.status_code == 200, r.text
    assert r.json() == {
        "user_id": "alice",
        "tenant_id": str(TENANT),
        "email": "a@x.test",
        "roles": ["tenant_admin"],
    }


def test_missing_token(client):
    _assert_401(client.get("/v1/me"), "TOKEN_MISSING")


def test_non_bearer_scheme(client):
    _assert_401(client.get("/v1/me", headers={"Authorization": "Basic dXNlcjpwYXNz"}), "TOKEN_MISSING")


def test_malformed_token(client):
    _assert_401(client.get("/v1/me", headers=bearer("not-a-jwt")), "TOKEN_MALFORMED")


def test_expired_token(client, tokens):
    past = int(time.time()) - 3600
    token = tokens.mint(tenant_id=TENANT, iat=past - 300, exp=past)
    _assert_401(client.get("/v1/me", headers=bearer(token)), "TOKEN_EXPIRED")


def test_wrong_audience(client, tokens):
    _assert_401(
        client.get("/v1/me", headers=bearer(tokens.mint(tenant_id=TENANT, aud="other-api"))),
        "TOKEN_INVALID_AUDIENCE",
    )


def test_wrong_issuer(client, tokens):
    token = tokens.mint(tenant_id=TENANT, iss="https://evil.example/realms/x")
    _assert_401(client.get("/v1/me", headers=bearer(token)), "TOKEN_INVALID_ISSUER")


def test_bad_signature(client, tokens):
    forged_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    token = tokens.mint(tenant_id=TENANT, signing_key=forged_key)
    _assert_401(client.get("/v1/me", headers=bearer(token)), "TOKEN_INVALID_SIGNATURE")


def test_tampered_tenant_claim_rejected(client, tokens):
    """Changing tid without re-signing must not switch tenants."""
    header, payload, signature = tokens.mint(tenant_id=TENANT).split(".")
    other = tokens.mint(tenant_id=uuid.uuid4()).split(".")[1]
    _assert_401(
        client.get("/v1/me", headers=bearer(f"{header}.{other}.{signature}")), "TOKEN_INVALID_SIGNATURE"
    )
    assert payload != other


def test_missing_tenant_claim(client, tokens):
    _assert_401(client.get("/v1/me", headers=bearer(tokens.mint(tenant_id=None))), "TOKEN_MISSING_TENANT")


def test_non_uuid_tenant_claim(client, tokens):
    _assert_401(client.get("/v1/me", headers=bearer(tokens.mint(tenant_id="acme"))), "TOKEN_MISSING_TENANT")


def test_missing_subject(client, tokens):
    _assert_401(
        client.get("/v1/me", headers=bearer(tokens.mint(tenant_id=TENANT, sub=None))), "TOKEN_MISSING_CLAIM"
    )


def test_unknown_key_id(client, tokens):
    _assert_401(
        client.get(
            "/v1/me",
            headers=bearer(
                tokens.mint(tenant_id=TENANT, kid="nope", signing_key=tokens.keys[tokens.active_kid])
            ),
        ),
        "TOKEN_UNKNOWN_KEY",
    )


def test_hs256_algorithm_confusion_rejected(client, tokens):
    import jwt

    token = jwt.encode(
        {"sub": "x", "tid": str(TENANT)}, "secret", algorithm="HS256", headers={"kid": tokens.active_kid}
    )
    _assert_401(client.get("/v1/me", headers=bearer(token)), "TOKEN_ALGORITHM_REJECTED")


def test_key_rotation_refreshes_jwks_once(client, tokens):
    assert client.get("/v1/me", headers=bearer(tokens.mint(tenant_id=TENANT))).status_code == 200
    fetches_before = tokens.fetches
    tokens.rotate()  # issuer publishes a new key and starts signing with it
    r = client.get("/v1/me", headers=bearer(tokens.mint(tenant_id=TENANT)))
    assert r.status_code == 200, r.text
    assert tokens.fetches == fetches_before + 1
    # Subsequent requests with the same new key use the cache.
    client.get("/v1/me", headers=bearer(tokens.mint(tenant_id=TENANT)))
    assert tokens.fetches == fetches_before + 1


def test_error_body_does_not_echo_token(client, tokens):
    token = tokens.mint(tenant_id=TENANT, aud="other-api")
    assert token not in client.get("/v1/me", headers=bearer(token)).text


def test_health_endpoints_stay_public(client):
    assert client.get("/healthz").status_code == 200
