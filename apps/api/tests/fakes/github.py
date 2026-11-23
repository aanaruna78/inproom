"""A small fake of the GitHub REST API (INPROOM-108).

In-process via `transport()` (httpx.MockTransport) for API tests, or as a real HTTP server via
`python -m tests.fakes.github --port 18090` for browser E2E (INPROOM-111). State is plain data so tests can
revoke tokens, change permissions, delete branches and inject failures.
"""

import itertools
import json
import re
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import unquote

import httpx

FULL = {"admin": False, "maintain": False, "push": False, "triage": False, "pull": True}


@dataclass
class FakeRepo:
    id: int
    full_name: str
    private: bool = False
    default_branch: str = "main"
    branches: dict[str, str] = field(default_factory=lambda: {"main": "a" * 40})


@dataclass
class FakeToken:
    login: str
    scopes: list[str] = field(default_factory=lambda: ["repo"])
    access: dict[int, dict[str, bool]] = field(default_factory=dict)  # repo id -> permissions
    valid: bool = True


class FakeGitHub:
    def __init__(self) -> None:
        self.repos: dict[int, FakeRepo] = {}
        self.tokens: dict[str, FakeToken] = {}
        self.hooks: dict[int, list[dict[str, Any]]] = {}
        self.failures: list[str] = []  # next responses: "timeout" | "500" | "ratelimit" | "redirect"
        self.calls: list[tuple[str, str]] = []
        self._ids = itertools.count(9000)

    # ---- setup helpers
    def add_repo(self, rid: int, full_name: str, **kw: Any) -> FakeRepo:
        self.repos[rid] = FakeRepo(rid, full_name, **kw)
        return self.repos[rid]

    def add_token(self, token: str, login: str, repos: dict[int, dict[str, bool]] | None = None, **kw: Any):
        self.tokens[token] = FakeToken(login, access=dict(repos or {}), **kw)
        return self.tokens[token]

    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self.handle)

    # ---- request handling
    def _json(self, status: int, body: Any, headers: dict[str, str] | None = None) -> httpx.Response:
        return httpx.Response(
            status, content=json.dumps(body), headers={"content-type": "application/json"} | (headers or {})
        )

    def _repo_json(self, repo: FakeRepo, perms: dict[str, bool]) -> dict[str, Any]:
        return {
            "id": repo.id,
            "full_name": repo.full_name,
            "private": repo.private,
            "default_branch": repo.default_branch,
            "permissions": perms,
        }

    def handle(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path.removeprefix("/api/v3")
        self.calls.append((request.method, path))
        if self.failures:
            failure = self.failures.pop(0)
            if failure == "timeout":
                raise httpx.ReadTimeout("fake timeout", request=request)
            if failure == "500":
                return self._json(502, {"message": "bad gateway"})
            if failure == "ratelimit":
                return self._json(
                    403, {"message": "rate limited"}, {"x-ratelimit-remaining": "0", "retry-after": "42"}
                )
            if failure == "redirect":
                return httpx.Response(302, headers={"location": "http://169.254.169.254/latest/meta-data"})
        auth = request.headers.get("authorization", "")
        token = self.tokens.get(auth.removeprefix("Bearer "))
        if token is None or not token.valid:
            return self._json(401, {"message": "Bad credentials"})
        visible = {rid: perms for rid, perms in token.access.items() if rid in self.repos}

        if path == "/user":
            return self._json(
                200,
                {"login": token.login, "id": abs(hash(token.login)) % 10**8},
                {"x-oauth-scopes": ", ".join(token.scopes)},
            )
        if path == "/user/repos":
            page = int(request.url.params.get("page", "1"))
            per_page = int(request.url.params.get("per_page", "30"))
            items = sorted(visible.items(), key=lambda kv: self.repos[kv[0]].full_name)
            chunk = items[(page - 1) * per_page : page * per_page]
            more = len(items) > page * per_page
            link = (
                {"link": f'<https://api.github.com/user/repos?page={page + 1}>; rel="next"'} if more else {}
            )
            return self._json(200, [self._repo_json(self.repos[r], p) for r, p in chunk], link)
        m = re.fullmatch(r"/repositories/(\d+)(/.*)?", path)
        if not m:
            return self._json(404, {"message": "Not Found"})
        rid, rest = int(m.group(1)), m.group(2) or ""
        if rid not in visible:
            return self._json(404, {"message": "Not Found"})  # GitHub hides inaccessible private repos
        repo = self.repos[rid]
        if rest == "" and request.method == "GET":
            return self._json(200, self._repo_json(repo, visible[rid]))
        bm = re.fullmatch(r"/branches/(.+)", rest)
        if bm and request.method == "GET":
            name = unquote(request.url.raw_path.decode().split("/branches/", 1)[1].split("?")[0])
            if name in repo.branches:
                return self._json(200, {"name": name, "commit": {"sha": repo.branches[name]}})
            return self._json(404, {"message": "Branch not found"})
        if rest == "/hooks":
            if not visible[rid].get("admin"):
                return self._json(404, {"message": "Not Found"})
            hooks = self.hooks.setdefault(rid, [])
            if request.method == "GET":
                return self._json(200, [{"id": h["id"], "config": {"url": h["url"]}} for h in hooks])
            if request.method == "POST":
                body = json.loads(request.content)
                hook = {
                    "id": next(self._ids),
                    "url": body["config"]["url"],
                    "secret": body["config"]["secret"],
                    "events": body["events"],
                }
                hooks.append(hook)
                return self._json(201, {"id": hook["id"]})
        hm = re.fullmatch(r"/hooks/(\d+)", rest)
        if hm and request.method == "DELETE":
            hooks = self.hooks.get(rid, [])
            before = len(hooks)
            self.hooks[rid] = [h for h in hooks if h["id"] != int(hm.group(1))]
            return (
                httpx.Response(204)
                if len(self.hooks[rid]) < before
                else self._json(404, {"message": "Not Found"})
            )
        return self._json(404, {"message": "Not Found"})


# Tokens used across API tests (fake values; shaped like fine-grained PATs).
VALID_TOKEN = "github_pat_11VALID000000000000000000000000000000000000000000000000000000000000000ab12"
OTHER_TOKEN = "github_pat_11OTHER000000000000000000000000000000000000000000000000000000000000000cd34"
BAD_TOKEN = "github_pat_11NOPE0000000000000000000000000000000000000000000000000000000000000000ef56"


def standard() -> FakeGitHub:
    """acme/api (private, readable), acme/web (readable but token can push), other/secret (no access)."""
    fake = FakeGitHub()
    fake.add_repo(1, "acme/api", private=True, branches={"main": "a" * 40, "release/1.0": "b" * 40})
    fake.add_repo(2, "acme/web", branches={"main": "c" * 40, "develop": "d" * 40})
    fake.add_repo(3, "other/secret", private=True)
    fake.add_token(VALID_TOKEN, "octo-bot", {1: dict(FULL), 2: FULL | {"push": True}})
    fake.add_token(OTHER_TOKEN, "octo-bot-2", {1: dict(FULL), 2: dict(FULL)})
    return fake
