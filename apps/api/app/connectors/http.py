"""Hardened HTTP core shared by every provider adapter (INPROOM-130; extracted from the GitHub adapter).

- bounded timeouts; at most `max_attempts` tries with jittered backoff on timeouts, transport errors and 5xx;
- rate limits surface immediately as RateLimited(retry_after) and are never slept on;
- 401 -> CredentialInvalid; 404 (and optionally 403) -> NotFoundOrNoAccess;
- redirects are never followed; credentials only ever travel in headers and never appear in messages.
The base URL must already have passed the egress allowlist (app.connectors.egress).
"""

import random
import time
from collections.abc import Callable
from typing import Any

import httpx

from app.connectors.base import (
    CredentialInvalid,
    NotFoundOrNoAccess,
    ProviderError,
    ProviderUnavailable,
    RateLimited,
)


class ProviderHttp:
    def __init__(
        self,
        provider_name: str,
        base_url: str,
        headers: dict[str, str],
        *,
        transport: httpx.BaseTransport | None = None,
        max_attempts: int = 3,
        sleep: Callable[[float], None] = time.sleep,
        forbidden_is_no_access: bool = False,
    ):
        self.name = provider_name
        self.max_attempts = max_attempts
        self.forbidden_is_no_access = forbidden_is_no_access
        self._sleep = sleep
        self._client = httpx.Client(
            base_url=base_url,
            headers={"User-Agent": "inproom-connector"} | headers,
            timeout=httpx.Timeout(10.0, connect=5.0),
            follow_redirects=False,
            transport=transport,
        )

    def close(self) -> None:
        self._client.close()

    def request(self, method: str, path: str, **kwargs: Any) -> httpx.Response:
        last = "no attempt made"
        for attempt in range(1, self.max_attempts + 1):
            try:
                response = self._client.request(method, path, **kwargs)
            except httpx.TimeoutException:
                last = "timeout"
            except httpx.TransportError as exc:
                last = f"transport error ({type(exc).__name__})"
            else:
                if response.status_code < 500:
                    return self._check(response)
                last = f"HTTP {response.status_code}"
            if attempt < self.max_attempts:
                self._sleep(min(0.25 * 2 ** (attempt - 1), 2.0) + random.uniform(0, 0.1))
        raise ProviderUnavailable(
            f"{self.name} did not respond successfully after {self.max_attempts} attempts ({last})"
        )

    def _check(self, response: httpx.Response) -> httpx.Response:
        status = response.status_code
        if status == 401:
            raise CredentialInvalid(f"{self.name} rejected the credential (invalid, expired or revoked)")
        if status == 429 or (status == 403 and response.headers.get("x-ratelimit-remaining") == "0"):
            raise RateLimited(_retry_after(response))
        if status == 404 or (status == 403 and self.forbidden_is_no_access):
            raise NotFoundOrNoAccess("not found, or the credential has no access")
        if 300 <= status < 400:
            raise ProviderError(f"{self.name} returned an unexpected redirect (not followed)")
        if status >= 400:
            raise ProviderError(f"{self.name} returned HTTP {status}")
        return response


class DownloadTooLarge(ProviderError):
    code = "DOWNLOAD_TOO_LARGE"


def _read_capped(response: httpx.Response, max_bytes: int) -> bytes:
    declared = response.headers.get("content-length")
    if declared and declared.isdigit() and int(declared) > max_bytes:
        raise DownloadTooLarge(f"{declared} bytes (limit {max_bytes})")
    out = bytearray()
    for chunk in response.iter_bytes():
        out += chunk
        if len(out) > max_bytes:
            raise DownloadTooLarge(f"more than {max_bytes} bytes (limit)")
    return bytes(out)


def download(
    http: "ProviderHttp",
    path: str,
    max_bytes: int,
    redirect_allowed: Callable[[str], bool],
) -> bytes:
    """GET a file with a size cap enforced while streaming. At most ONE redirect is followed, only to a URL
    `redirect_allowed` accepts, and that request carries no credential (a fresh client, no auth header)."""
    client = http._client
    with client.stream("GET", path) as response:
        if 300 <= response.status_code < 400:
            target = str(response.url.join(response.headers.get("location", "")))
        else:
            http._check(response)
            return _read_capped(response, max_bytes)
    if not redirect_allowed(target):
        raise ProviderError(f"{http.name} redirected a download to a host that is not allowed")
    with (
        httpx.Client(
            headers={"User-Agent": "inproom-connector"},
            timeout=httpx.Timeout(30.0, connect=5.0),
            follow_redirects=False,
            transport=client._transport,
        ) as plain,
        plain.stream("GET", target) as response,
    ):
        if 300 <= response.status_code < 400:
            raise ProviderError(f"{http.name} download redirected more than once (not followed)")
        if response.status_code == 404:
            raise NotFoundOrNoAccess("the file is gone")
        if response.status_code >= 400:
            raise ProviderUnavailable(f"{http.name} file service returned HTTP {response.status_code}")
        return _read_capped(response, max_bytes)


def _retry_after(response: httpx.Response) -> int:
    retry = response.headers.get("retry-after")
    if retry is None and response.headers.get("x-ratelimit-reset"):
        try:
            return max(1, int(response.headers["x-ratelimit-reset"]) - int(time.time()))
        except ValueError:
            return 60
    try:
        return max(1, int(retry or 60))
    except ValueError:  # an HTTP-date Retry-After: be conservative rather than parse provider dates
        return 60
