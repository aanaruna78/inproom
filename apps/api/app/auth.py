"""OIDC bearer-token authentication. Identity and tenant come only from verified token claims."""

import json
import threading
import time
import urllib.request
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from functools import lru_cache
from typing import Annotated, Any

import jwt
from fastapi import Depends, Request
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from app.config import get_settings

ALGORITHMS = ["RS256"]


class AuthError(Exception):
    """Raised for any authentication failure; rendered as 401 problem+json."""

    def __init__(self, code: str, detail: str):
        super().__init__(detail)
        self.code = code
        self.detail = detail


@dataclass(frozen=True)
class Principal:
    user_id: str
    tenant_id: uuid.UUID
    email: str | None = None
    roles: frozenset[str] = field(default_factory=frozenset)


class JwksCache:
    """Caches the issuer's signing keys; refetches at most once per `min_refresh_s` on an unknown kid."""

    def __init__(self, fetch: Callable[[], dict[str, Any]], min_refresh_s: float = 10.0):
        self._fetch = fetch
        self._min_refresh_s = min_refresh_s
        self._keys: dict[str, jwt.PyJWK] = {}
        self._last_refresh = 0.0
        self._lock = threading.Lock()

    def _refresh(self) -> None:
        document = self._fetch()
        self._keys = {
            k["kid"]: jwt.PyJWK(k)
            for k in document.get("keys", [])
            if k.get("kid") and k.get("use", "sig") == "sig"
        }
        self._last_refresh = time.monotonic()

    def get(self, kid: str) -> jwt.PyJWK:
        with self._lock:
            if kid not in self._keys and time.monotonic() - self._last_refresh >= self._min_refresh_s:
                try:
                    self._refresh()
                except Exception as exc:  # network/parse errors must not leak as 500s
                    raise AuthError("JWKS_UNAVAILABLE", "Signing keys could not be retrieved") from exc
            if kid not in self._keys:
                raise AuthError("TOKEN_UNKNOWN_KEY", "Token signed with an unknown key")
            return self._keys[kid]


def _http_fetch(url: str, timeout: int) -> Callable[[], dict[str, Any]]:
    def fetch() -> dict[str, Any]:
        with urllib.request.urlopen(url, timeout=timeout) as resp:  # noqa: S310 - configured issuer URL
            return json.loads(resp.read())

    return fetch


@lru_cache
def get_jwks_cache() -> JwksCache:
    s = get_settings()
    return JwksCache(_http_fetch(s.jwks_url, s.oidc_jwks_timeout_s))


def verify_token(token: str, jwks: JwksCache) -> Principal:
    s = get_settings()
    try:
        header = jwt.get_unverified_header(token)
    except jwt.PyJWTError as exc:
        raise AuthError("TOKEN_MALFORMED", "Bearer token is not a valid JWT") from exc
    if header.get("alg") not in ALGORITHMS:
        raise AuthError("TOKEN_ALGORITHM_REJECTED", "Token algorithm is not accepted")
    key = jwks.get(header.get("kid", ""))
    try:
        claims = jwt.decode(
            token,
            key=key,
            algorithms=ALGORITHMS,
            audience=s.oidc_audience,
            issuer=s.oidc_issuer,
            leeway=s.oidc_leeway_s,
            options={"require": ["exp", "iat", "iss", "aud", "sub"]},
        )
    except jwt.ExpiredSignatureError as exc:
        raise AuthError("TOKEN_EXPIRED", "Token has expired") from exc
    except jwt.InvalidAudienceError as exc:
        raise AuthError("TOKEN_INVALID_AUDIENCE", "Token audience is not accepted") from exc
    except jwt.InvalidIssuerError as exc:
        raise AuthError("TOKEN_INVALID_ISSUER", "Token issuer is not accepted") from exc
    except jwt.InvalidSignatureError as exc:
        raise AuthError("TOKEN_INVALID_SIGNATURE", "Token signature is invalid") from exc
    except jwt.MissingRequiredClaimError as exc:
        raise AuthError("TOKEN_MISSING_CLAIM", f"Token is missing required claim '{exc.claim}'") from exc
    except jwt.PyJWTError as exc:
        raise AuthError("TOKEN_INVALID", "Token is invalid") from exc

    raw_tid = claims.get("tid")
    try:
        tenant_id = uuid.UUID(str(raw_tid)) if raw_tid else None
    except ValueError:
        tenant_id = None
    if tenant_id is None:
        raise AuthError("TOKEN_MISSING_TENANT", "Token has no valid tenant (tid) claim")

    roles = frozenset((claims.get("realm_access") or {}).get("roles") or [])
    return Principal(user_id=claims["sub"], tenant_id=tenant_id, email=claims.get("email"), roles=roles)


_bearer = HTTPBearer(auto_error=False, description="OIDC access token issued for audience inproom-api")


def get_principal(
    request: Request,
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(_bearer)],
    jwks: Annotated[JwksCache, Depends(get_jwks_cache)],
) -> Principal:
    if credentials is None or credentials.scheme.lower() != "bearer" or not credentials.credentials:
        raise AuthError("TOKEN_MISSING", "Bearer token required")
    principal = verify_token(credentials.credentials, jwks)
    request.state.principal = principal
    return principal


CurrentPrincipal = Annotated[Principal, Depends(get_principal)]
