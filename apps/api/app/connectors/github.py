"""GitHub REST adapter (INPROOM-108). Works for github.com and GitHub Enterprise (base_url .../api/v3).

Hardening:
- bounded timeouts; at most `max_attempts` tries with jittered backoff on timeouts, transport errors and 5xx;
- rate limits surface immediately as RateLimited(retry_after) and are never slept on;
- 401 -> CredentialInvalid; 404 -> NotFoundOrNoAccess (GitHub hides private repos as 404);
- redirects are never followed; the token only ever goes in the Authorization header, never in messages.
"""

import random
import re
import time
from collections.abc import Callable
from typing import Any
from urllib.parse import quote

import httpx

from app.connectors.base import (
    Account,
    CredentialInvalid,
    NotFoundOrNoAccess,
    ProviderError,
    ProviderUnavailable,
    RateLimited,
    Repository,
)
from app.connectors.egress import validate_base_url
from app.connectors.vault import Secret

API_VERSION = "2022-11-28"
MAX_PAGES = 10  # hard cap when walking paginated lists
_NEXT = re.compile(r'<[^>]+[?&]page=(\d+)[^>]*>;\s*rel="next"')


class GitHubProvider:
    def __init__(
        self,
        base_url: str,
        token: Secret,
        *,
        transport: httpx.BaseTransport | None = None,
        max_attempts: int = 3,
        sleep: Callable[[float], None] = time.sleep,
    ):
        self.base_url = validate_base_url(base_url)
        self.max_attempts = max_attempts
        self._sleep = sleep
        self._client = httpx.Client(
            base_url=self.base_url,
            headers={
                "Authorization": f"Bearer {token.reveal()}",
                "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": API_VERSION,
                "User-Agent": "inproom-connector",
            },
            timeout=httpx.Timeout(10.0, connect=5.0),
            follow_redirects=False,
            transport=transport,
        )

    def close(self) -> None:
        self._client.close()

    # ------------------------------------------------------------ transport

    def _request(self, method: str, path: str, **kwargs: Any) -> httpx.Response:
        last: str = "no attempt made"
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
            f"GitHub did not respond successfully after {self.max_attempts} attempts ({last})"
        )

    @staticmethod
    def _check(response: httpx.Response) -> httpx.Response:
        status = response.status_code
        if status == 401:
            raise CredentialInvalid("GitHub rejected the credential (invalid, expired or revoked)")
        if status == 429 or (status == 403 and response.headers.get("x-ratelimit-remaining") == "0"):
            retry = response.headers.get("retry-after")
            if retry is None and response.headers.get("x-ratelimit-reset"):
                retry = str(max(1, int(response.headers["x-ratelimit-reset"]) - int(time.time())))
            raise RateLimited(int(retry or 60))
        if status == 404:
            raise NotFoundOrNoAccess("not found, or the credential has no access")
        if 300 <= status < 400:
            raise ProviderError("GitHub returned an unexpected redirect (not followed)")
        if status >= 400:
            raise ProviderError(f"GitHub returned HTTP {status}")
        return response

    def _pages(self, path: str, params: dict[str, Any], start: int = 1, limit: int = MAX_PAGES):
        page = start
        for _ in range(limit):
            response = self._request("GET", path, params=params | {"page": page})
            yield response.json(), bool(_NEXT.search(response.headers.get("link", "")))
            if not _NEXT.search(response.headers.get("link", "")):
                return
            page += 1

    # ------------------------------------------------------------ API

    def whoami(self) -> Account:
        response = self._request("GET", "/user")
        data = response.json()
        scopes = [s.strip() for s in response.headers.get("x-oauth-scopes", "").split(",") if s.strip()]
        return Account(login=data["login"], id=str(data["id"]), scopes=scopes)

    @staticmethod
    def _repo(data: dict[str, Any]) -> Repository:
        return Repository(
            id=str(data["id"]),
            full_name=data["full_name"],
            private=bool(data.get("private")),
            default_branch=data.get("default_branch") or "main",
            permissions={k: bool(v) for k, v in (data.get("permissions") or {}).items()},
        )

    def list_repositories(self, q: str | None = None, page: int = 1) -> tuple[list[Repository], bool]:
        params = {
            "per_page": 50,
            "sort": "full_name",
            "affiliation": "owner,collaborator,organization_member",
        }
        if not q:
            data, has_next = next(self._pages("/user/repos", params, start=page, limit=1))
            return [self._repo(r) for r in data], has_next
        needle = q.lower()
        found = [
            self._repo(r)
            for data, _ in self._pages("/user/repos", params)
            for r in data
            if needle in r["full_name"].lower()
        ]
        return found, False

    def get_repository(self, repo_id: str) -> Repository:
        return self._repo(self._request("GET", f"/repositories/{int(repo_id)}").json())

    def branch_exists(self, repo_id: str, branch: str) -> bool:
        try:
            self._request("GET", f"/repositories/{int(repo_id)}/branches/{quote(branch, safe='')}")
            return True
        except NotFoundOrNoAccess:
            return False

    def find_hook(self, repo_id: str, url: str) -> str | None:
        for data, _ in self._pages(f"/repositories/{int(repo_id)}/hooks", {"per_page": 100}):
            for hook in data:
                if (hook.get("config") or {}).get("url") == url:
                    return str(hook["id"])
        return None

    def create_hook(self, repo_id: str, url: str, secret: str, events: list[str]) -> str:
        body = {
            "name": "web",
            "active": True,
            "events": events,
            "config": {"url": url, "content_type": "json", "secret": secret, "insecure_ssl": "0"},
        }
        return str(self._request("POST", f"/repositories/{int(repo_id)}/hooks", json=body).json()["id"])

    def delete_hook(self, repo_id: str, hook_id: str) -> None:
        try:
            self._request("DELETE", f"/repositories/{int(repo_id)}/hooks/{int(hook_id)}")
        except NotFoundOrNoAccess:
            pass  # already gone: deletion is idempotent
