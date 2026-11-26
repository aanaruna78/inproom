"""Mint RS256 tokens with a local key pair and serve them through an in-memory JWKS (no Keycloak needed)."""

import json
import time
import uuid
from typing import Any

import jwt
from cryptography.hazmat.primitives.asymmetric import rsa

from app.auth import JwksCache
from app.config import get_settings

_FIRST_KEY: tuple[str, rsa.RSAPrivateKey] | None = None


def _first_key() -> tuple[str, rsa.RSAPrivateKey]:
    """One initial key pair for every factory in the session: generating a 2048-bit RSA key takes ~35 ms, paid
    by every test otherwise; and shared security worlds stay usable across tests (their tokens verify)."""
    global _FIRST_KEY
    if _FIRST_KEY is None:
        _FIRST_KEY = (uuid.uuid4().hex[:12], rsa.generate_private_key(public_exponent=65537, key_size=2048))
    return _FIRST_KEY


class TokenFactory:
    def __init__(self) -> None:
        self.keys: dict[str, rsa.RSAPrivateKey] = {}
        self.published: set[str] = set()
        self.fetches = 0
        kid, key = _first_key()
        self.keys[kid] = key
        self.published.add(kid)
        self.active_kid = kid

    def rotate(self) -> str:
        """Create a new signing key, publish it, and make it the active key."""
        kid = uuid.uuid4().hex[:12]
        self.keys[kid] = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        self.published.add(kid)
        self.active_kid = kid
        return kid

    def jwks(self) -> dict[str, Any]:
        self.fetches += 1
        keys = []
        for kid in self.published:
            jwk = json.loads(jwt.algorithms.RSAAlgorithm.to_jwk(self.keys[kid].public_key()))
            keys.append(jwk | {"kid": kid, "use": "sig", "alg": "RS256"})
        return {"keys": keys}

    def cache(self) -> JwksCache:
        return JwksCache(self.jwks, min_refresh_s=0)

    def mint(
        self,
        *,
        tenant_id: uuid.UUID | str | None,
        user_id: str = "user-1",
        roles: list[str] | None = None,
        email: str | None = "user@example.test",
        kid: str | None = None,
        signing_key: rsa.RSAPrivateKey | None = None,
        **overrides: Any,
    ) -> str:
        s = get_settings()
        now = int(time.time())
        claims: dict[str, Any] = {
            "iss": s.oidc_issuer,
            "aud": s.oidc_audience,
            "sub": user_id,
            "iat": now,
            "exp": now + 300,
            "email": email,
            "realm_access": {"roles": roles or []},
        }
        if tenant_id is not None:
            claims["tid"] = str(tenant_id)
        claims.update(overrides)
        claims = {k: v for k, v in claims.items() if v is not None}
        kid = kid or self.active_kid
        key = signing_key or self.keys[kid]
        return jwt.encode(claims, key, algorithm="RS256", headers={"kid": kid})


def bearer(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}
