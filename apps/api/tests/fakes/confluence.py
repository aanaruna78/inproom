"""A small fake of the Confluence content REST API (INPROOM-134), Cloud (/wiki/rest/api) and DC (/rest/api).

In-process via `transport()` (httpx.MockTransport) for API tests, or as a real HTTP server via
`python -m tests.fakes.confluence --port 18092` for browser E2E. State is plain data so tests can edit,
delete, restore and restrict pages, revoke space access and inject failures.
"""

import base64
import json
import re
from dataclasses import dataclass, field
from typing import Any

import httpx

EMAIL = "bot@acme.example"
VALID_TOKEN = "ATATT3xFfGF0CONFLUENCE0000000000000000000000000000000000000000000000000=CF12AB34"
READONLY_TOKEN = "ATATT3xFfGF0CONFREADER0000000000000000000000000000000000000000000000000=CF56CD78"
BAD_TOKEN = "ATATT3xFfGF0CONFNOPE00000000000000000000000000000000000000000000000000000=00000000"
DC_PAT = "MzQ1Njc4OTAxMjM0OkFAKECONFLUENCEDCPATaBcDeFgHiJkLmNoPqRsTuVwXyZ01"


@dataclass
class FakePage:
    id: int
    space: str
    title: str
    parent: int | None = None
    status: str = "current"  # current | draft | trashed | archived
    labels: list[str] = field(default_factory=list)
    users: list[str] = field(default_factory=list)  # own read restrictions
    groups: list[str] = field(default_factory=list)
    attachments: list[dict[str, Any]] = field(default_factory=list)
    versions: list[dict[str, Any]] = field(default_factory=list)  # [{number, body, by, when, message}]

    def add_version(self, body: str, by: str = "Ada", message: str = "", when: str | None = None) -> None:
        n = len(self.versions) + 1
        self.versions.append(
            {
                "number": n,
                "body": body,
                "by": by,
                "message": message,
                "when": when or f"2026-11-{10 + n:02d}T09:00:00.000Z",
            }
        )


@dataclass
class FakeUser:
    email: str
    spaces: dict[str, set[str]]  # space key -> operations ("read", "update", ...)
    valid: bool = True


class FakeConfluence:
    def __init__(self) -> None:
        self.spaces: dict[str, dict[str, Any]] = {}
        self.pages: dict[int, FakePage] = {}
        self.users: dict[str, FakeUser] = {}
        self.media_requests: list[dict[str, Any]] = []  # downloads that reached another host (INPROOM-166)
        self.failures: list[str] = []  # "timeout" | "500" | "ratelimit" | "redirect"
        self.fail_after: int | None = None  # inject the next failure only after this many more calls
        self.calls: list[tuple[str, str]] = []

    # ---- setup / control helpers
    def add_space(self, sid: int, key: str, name: str) -> None:
        self.spaces[key] = {"id": sid, "key": key, "name": name, "type": "global", "status": "current"}

    def add_page(self, pid: int, space: str, title: str, body: str = "", **kw: Any) -> FakePage:
        page = FakePage(pid, space, title, **kw)
        page.add_version(body or f"<p>{title}</p>")
        self.pages[pid] = page
        return page

    def add_user(self, token: str, email: str, spaces: dict[str, set[str]]) -> None:
        self.users[token] = FakeUser(email, {k: set(v) for k, v in spaces.items()})

    def edit(self, pid: int, body: str, message: str = "edited") -> None:
        self.pages[pid].add_version(body, message=message)

    def revoke_space(self, token: str, key: str) -> None:
        self.users[token].spaces.pop(key, None)

    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self.handle)

    # ---- helpers
    @staticmethod
    def _json(status: int, body: Any, headers: dict[str, str] | None = None) -> httpx.Response:
        return httpx.Response(
            status, content=json.dumps(body), headers={"content-type": "application/json"} | (headers or {})
        )

    def _user(self, request: httpx.Request) -> FakeUser | None:
        auth = request.headers.get("authorization", "")
        if auth.startswith("Basic "):
            try:
                email, _, token = base64.b64decode(auth[6:]).decode().partition(":")
            except ValueError:
                return None
            user = self.users.get(token)
            return user if user and user.valid and user.email == email else None
        user = self.users.get(auth.removeprefix("Bearer "))
        return user if user and user.valid else None

    def _ancestors(self, page: FakePage) -> list[FakePage]:
        chain, cur = [], page
        while cur.parent is not None and cur.parent in self.pages:
            cur = self.pages[cur.parent]
            chain.append(cur)
        return list(reversed(chain))

    def _space_json(self, key: str, user: FakeUser, expand: str) -> dict[str, Any]:
        out = dict(self.spaces[key])
        if "operations" in expand:
            out["operations"] = [
                {"operation": op, "targetType": "space"} for op in sorted(user.spaces.get(key, set()))
            ]
        return out

    @staticmethod
    def _restrict(p: FakePage) -> dict[str, Any]:
        return {
            "read": {
                "restrictions": {
                    "user": {"results": [{"accountId": u, "displayName": u} for u in p.users]},
                    "group": {"results": [{"name": g} for g in p.groups]},
                }
            }
        }

    # ---- Cloud REST v2 (cursor paging)
    @staticmethod
    def _cursored(results: list[Any], params: httpx.QueryParams, path: str) -> dict[str, Any]:
        start = int(base64.urlsafe_b64decode(params["cursor"]).decode()) if params.get("cursor") else 0
        limit = int(params.get("limit", "25"))
        out: dict[str, Any] = {"results": results[start : start + limit], "_links": {}}
        if start + limit < len(results):
            cursor = base64.urlsafe_b64encode(str(start + limit).encode()).decode()
            rest = "&".join(f"{k}={v}" for k, v in params.items() if k != "cursor")
            out["_links"]["next"] = f"/wiki/api/v2{path}?{rest}&cursor={cursor}"
        return out

    def _v2_page(self, page: FakePage, version: dict[str, Any], params: httpx.QueryParams) -> dict[str, Any]:
        out: dict[str, Any] = {
            "id": str(page.id),
            "status": page.status,
            "title": page.title,
            "spaceId": str(self.spaces[page.space]["id"]),
            "parentId": str(page.parent) if page.parent else None,
            "parentType": "page" if page.parent else None,
            "authorId": page.versions[0]["by"],
            "version": {
                "number": version["number"],
                "message": version["message"],
                "createdAt": version["when"],
                "authorId": version["by"],
            },
            "_links": {"webui": f"/spaces/{page.space}/pages/{page.id}"},
        }
        if params.get("body-format") == "storage":
            out["body"] = {"storage": {"value": version["body"], "representation": "storage"}}
        if params.get("include-labels") == "true":
            out["labels"] = {
                "results": [{"id": label, "name": label, "prefix": "global"} for label in page.labels]
            }
        return out

    def _v2(self, path: str, params: httpx.QueryParams, user: "FakeUser") -> httpx.Response:
        readable = {k for k, ops in user.spaces.items() if "read" in ops and k in self.spaces}
        by_id = {str(sp["id"]): k for k, sp in self.spaces.items()}
        if path == "/spaces":
            keys = sorted(readable)
            if params.get("keys"):
                keys = [k for k in keys if k in params["keys"].split(",")]
            return self._json(200, self._cursored([dict(self.spaces[k]) for k in keys], params, path))
        sm = re.fullmatch(r"/spaces/(\d+)/(operations|pages)", path)
        if sm:
            key = by_id.get(sm.group(1))
            if key not in readable:
                return self._json(404, {"message": "space not found"})
            if sm.group(2) == "operations":
                ops = [{"operation": op, "targetType": "space"} for op in sorted(user.spaces[key])]
                return self._json(200, {"operations": ops})
            status = params.get("status", "current")
            pages = sorted(
                (
                    p
                    for p in self.pages.values()
                    if p.space == key and p.status == status and self._visible(p, user)
                ),
                key=lambda p: p.id,
            )
            return self._json(
                200, self._cursored([self._v2_page(p, p.versions[-1], params) for p in pages], params, path)
            )
        pm = re.fullmatch(r"/pages/(\d+)(/labels|/attachments|/versions|/ancestors)?", path)
        if pm:
            page = self.pages.get(int(pm.group(1)))
            if page is None or not self._visible(page, user) or page.status == "trashed":
                return self._json(404, {"message": "page not found"})
            sub = pm.group(2)
            if sub == "/labels":
                items = [{"id": label, "name": label, "prefix": "global"} for label in page.labels]
            elif sub == "/attachments":
                items = [
                    {
                        "id": a["id"],
                        "title": a["title"],
                        "mediaType": (a.get("metadata") or {}).get("mediaType"),
                        "fileSize": (a.get("extensions") or {}).get("fileSize"),
                        "version": a.get("version"),
                    }
                    for a in page.attachments
                ]
            elif sub == "/versions":
                items = [
                    {
                        "number": v["number"],
                        "message": v["message"],
                        "createdAt": v["when"],
                        "authorId": v["by"],
                    }
                    for v in page.versions
                ]
            elif sub == "/ancestors":
                items = [{"id": str(a.id), "type": "page"} for a in self._ancestors(page)]
            else:
                wanted = params.get("version")
                version = (
                    page.versions[-1]
                    if wanted is None
                    else next((v for v in page.versions if v["number"] == int(wanted)), None)
                )
                if version is None:
                    return self._json(404, {"message": "No such version"})
                return self._json(200, self._v2_page(page, version, params))
            return self._json(200, self._cursored(items, params, path))
        return self._json(404, {"message": "not found"})

    def _page_json(self, page: FakePage, version: dict[str, Any], prefix: str) -> dict[str, Any]:
        restrict = self._restrict

        return {
            "id": str(page.id),
            "type": "page",
            "status": page.status,
            "title": page.title,
            "space": {"key": page.space, "id": self.spaces[page.space]["id"]},
            "version": {
                "number": version["number"],
                "by": {"displayName": version["by"]},
                "when": version["when"],
                "message": version["message"],
            },
            "ancestors": [{"id": str(a.id), "title": a.title} for a in self._ancestors(page)],
            "body": {"storage": {"value": version["body"], "representation": "storage"}},
            "metadata": {"labels": {"results": [{"name": label} for label in page.labels]}},
            "children": {"attachment": {"results": page.attachments}},
            "restrictions": restrict(page),
            "_links": {"webui": f"/spaces/{page.space}/pages/{page.id}"},
        }

    def _visible(self, page: FakePage, user: FakeUser) -> bool:
        """Real Confluence hides pages restricted to others (the bot is in group eng-leads only if listed)."""
        if "read" not in user.spaces.get(page.space, set()):
            return False
        for p in [*self._ancestors(page), page]:
            if (p.users or p.groups) and user.email not in p.users and "bots" not in p.groups:
                return False
        return True

    @staticmethod
    def _paged(results: list[Any], params: httpx.QueryParams, path: str) -> dict[str, Any]:
        start, limit = int(params.get("start", "0")), int(params.get("limit", "25"))
        chunk = results[start : start + limit]
        out: dict[str, Any] = {
            "results": chunk,
            "start": start,
            "limit": limit,
            "size": len(chunk),
            "_links": {},
        }
        if start + limit < len(results):
            rest = "&".join(f"{k}={v}" for k, v in params.items() if k != "start")
            out["_links"]["next"] = f"{path}?{rest}&start={start + limit}"
        return out

    # ---- request handling
    def add_attachment(
        self,
        pid: int,
        aid: str,
        title: str,
        data: bytes,
        media_type: str,
        *,
        version: int = 1,
        redirect: str | None = None,
    ) -> dict[str, Any]:
        """An attachment with downloadable bytes (INPROOM-166). `redirect`: where the download redirects to
        (Cloud sends downloads to its media service)."""
        page = self.pages[pid]
        page.attachments = [a for a in page.attachments if a["id"] != aid]
        att = {
            "id": aid,
            "title": title,
            "metadata": {"mediaType": media_type},
            "extensions": {"fileSize": len(data)},
            "version": {"number": version},
            "_bytes": data,
            "_redirect": redirect,
        }
        page.attachments.append(att)
        return att

    def _attachment(self, aid: str) -> dict[str, Any] | None:
        for page in self.pages.values():
            for att in page.attachments:
                if att["id"] == aid:
                    return att
        return None

    def handle(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        self.calls.append((request.method, path))
        fm = re.fullmatch(r"/file/([A-Za-z0-9]+)/binary", path)
        if fm:
            # Atlassian's media service (where Cloud downloads redirect): no auth expected.
            self.media_requests.append({"host": request.url.host, "headers": dict(request.headers)})
            att = self._attachment(fm.group(1))
            if att is None:
                return self._json(404, {"message": "no such file"})
            return httpx.Response(200, content=att["_bytes"])
        if self.failures and (self.fail_after is None or self.fail_after <= 0):
            failure = self.failures.pop(0)
            if failure == "timeout":
                raise httpx.ReadTimeout("fake timeout", request=request)
            if failure == "500":
                return self._json(503, {"message": "unavailable"})
            if failure == "ratelimit":
                return self._json(429, {"message": "rate limited"}, {"retry-after": "42"})
            if failure == "redirect":
                return httpx.Response(302, headers={"location": "http://169.254.169.254/latest/meta-data"})
        if self.fail_after is not None:
            self.fail_after -= 1
        user = self._user(request)
        if user is None:
            return self._json(401, {"message": "Unauthorized"})
        if path.startswith("/wiki/api/v2/"):
            return self._v2(path.removeprefix("/wiki/api/v2"), request.url.params, user)
        m = re.fullmatch(r"(/wiki)?/rest/api(/.*)", path)
        if not m:
            return self._json(404, {"message": "not found"})
        prefix, rest, params = (m.group(1) or "") + "/rest/api", m.group(2), request.url.params
        expand = params.get("expand", "")
        dm = re.fullmatch(r"/content/(\d+)/child/attachment/([^/]+)/download", rest)
        if dm:
            page = self.pages.get(int(dm.group(1)))
            att = self._attachment(dm.group(2))
            if page is None or not self._visible(page, user) or att is None or att not in page.attachments:
                return self._json(404, {"message": "No attachment found"})
            if att.get("_redirect"):
                return httpx.Response(302, headers={"location": att["_redirect"]})
            return httpx.Response(
                200, content=att["_bytes"], headers={"content-length": str(len(att["_bytes"]))}
            )
        rm = re.fullmatch(r"/content/(\d+)/restriction/byOperation/read", rest)
        if rm:  # v1 on Cloud too (v2 has no restrictions API)
            page = self.pages.get(int(rm.group(1)))
            if page is None or not self._visible(page, user):
                return self._json(404, {"message": "No content found"})
            return self._json(
                200, {"operation": "read", "restrictions": self._restrict(page)["read"]["restrictions"]}
            )
        if m.group(1) and rest != "/user/current":
            # Cloud must use REST v2 for spaces and pages (INPROOM-138): refuse the v1 content API on /wiki.
            return self._json(410, {"message": "fake Cloud: use /wiki/api/v2 for this resource"})

        if rest == "/user/current":
            return self._json(
                200, {"accountId": "acc-bot", "email": user.email, "displayName": "INPROOM bot"}
            )
        if rest == "/space":
            keys = sorted(k for k, ops in user.spaces.items() if "read" in ops and k in self.spaces)
            return self._json(
                200, self._paged([self._space_json(k, user, expand) for k in keys], params, prefix + rest)
            )
        sm = re.fullmatch(r"/space/([^/]+)", rest)
        if sm:
            key = sm.group(1)
            if key not in self.spaces or "read" not in user.spaces.get(key, set()):
                return self._json(404, {"message": "No space with key"})
            return self._json(200, self._space_json(key, user, expand))
        if rest == "/content":
            key, status = params.get("spaceKey", ""), params.get("status", "current")
            if key not in self.spaces or "read" not in user.spaces.get(key, set()):
                return self._json(404, {"message": "No space with key"})
            pages = sorted(
                (
                    p
                    for p in self.pages.values()
                    if p.space == key and p.status == status and self._visible(p, user)
                ),
                key=lambda p: p.id,
            )
            return self._json(
                200,
                self._paged(
                    [self._page_json(p, p.versions[-1], prefix) for p in pages], params, prefix + rest
                ),
            )
        cm = re.fullmatch(r"/content/(\d+)(/version)?", rest)
        if cm:
            page = self.pages.get(int(cm.group(1)))
            if page is None or not self._visible(page, user) or page.status == "trashed":
                return self._json(404, {"message": "No content found"})
            if cm.group(2):
                versions = [
                    {
                        "number": v["number"],
                        "by": {"displayName": v["by"]},
                        "when": v["when"],
                        "message": v["message"],
                    }
                    for v in page.versions
                ]
                return self._json(200, self._paged(versions, params, prefix + rest))
            wanted = params.get("version")
            version = (
                page.versions[-1]
                if wanted is None
                else next((v for v in page.versions if v["number"] == int(wanted)), None)
            )
            if version is None:
                return self._json(404, {"message": "No such version"})
            return self._json(200, self._page_json(page, version, prefix))
        return self._json(404, {"message": "not found"})


# ---------------------------------------------------------------- the standard site

ARCH_V1 = "<h1>Architecture</h1><p>Monolith first.</p>"
ARCH_V3 = (
    "<h1>Architecture</h1><p>Services:</p><ul><li>api</li><li>web</li></ul>"
    '<ac:structured-macro ac:name="toc"/>'
    "<table><tr><th>Svc</th><th>Owner</th></tr><tr><td>api</td><td>Ada</td></tr>"
    "</table><pre>make up</pre>"
)


def standard() -> FakeConfluence:
    """ENG: approved tree (100 home, 101 architecture with 3 versions + attachment, 102 runbooks), a page
    restricted to the bot's group (103) and its child (104), a draft (105), a trashed page (106), an
    unlabelled page (107) and 65 notes (200-264) so listings span several provider pages. 110 is restricted
    to someone else (invisible). HR: one page. SECRET: never readable. VALID_TOKEN can also write ENG."""
    fake = FakeConfluence()
    fake.add_space(1, "ENG", "Engineering")
    fake.add_space(2, "HR", "People")
    fake.add_space(3, "SECRET", "Secret")
    fake.add_page(100, "ENG", "ENG Home", labels=["approved"])
    arch = fake.add_page(101, "ENG", "Architecture", ARCH_V1, parent=100, labels=["approved", "design"])
    arch.add_version("<h1>Architecture</h1><p>Two services.</p>", by="Grace", message="split")
    arch.add_version(ARCH_V3, by="Ada", message="detail")
    arch.attachments = [
        {
            "id": "att9001",
            "title": "diagram.png",
            "metadata": {"mediaType": "image/png"},
            "extensions": {"fileSize": 2048},
            "version": {"number": 1},
        }
    ]
    fake.add_page(102, "ENG", "Runbooks", "<p>Restart the api.</p>", parent=100, labels=["approved"])
    fake.add_page(
        103, "ENG", "Leads only", "<p>Org plans.</p>", parent=100, labels=["approved"], groups=["bots"]
    )
    fake.add_page(104, "ENG", "Leads child", "<p>Details.</p>", parent=103, labels=["approved"])
    fake.add_page(105, "ENG", "Draft idea", status="draft", parent=100, labels=["approved"])
    fake.add_page(106, "ENG", "Old page", status="trashed", parent=100, labels=["approved"])
    fake.add_page(107, "ENG", "Scratch notes", parent=100)
    fake.add_page(110, "ENG", "Someone else's", parent=100, users=["acc-someone"])
    for i in range(65):
        fake.add_page(200 + i, "ENG", f"Note {i:02d}", parent=102 if i < 3 else 100)
    fake.add_page(300, "HR", "HR policy", labels=["approved"])
    fake.add_page(400, "SECRET", "Secret page")
    fake.add_user(VALID_TOKEN, EMAIL, {"ENG": {"read", "update", "create"}, "HR": {"read"}})
    fake.add_user(READONLY_TOKEN, "reader@acme.example", {"ENG": {"read"}})
    fake.add_user(DC_PAT, "dc-bot", {"ENG": {"read"}})
    return fake


def serve(port: int) -> None:
    """Run the standard fake as a local HTTP server for browser E2E. NEVER expose it: test-only.

    Control endpoints (test-only): POST /_fake/reset, POST /_fake/edit {"page_id","body"},
    POST /_fake/space-access {"token","space","grant"}, POST /_fake/token {"token","valid"},
    GET /_fake/health.
    """
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

    state = {"fake": standard()}

    class Handler(BaseHTTPRequestHandler):
        def _send(self, status: int, body: bytes, headers: dict[str, str]) -> None:
            self.send_response(status)
            for k, v in headers.items():
                if k.lower() not in ("content-length", "transfer-encoding", "connection"):
                    self.send_header(k, v)
            self.send_header("content-length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _handle(self) -> None:
            length = int(self.headers.get("content-length") or 0)
            body = self.rfile.read(length) if length else b""
            if self.path.startswith("/_fake/"):
                return self._control(body)
            request = httpx.Request(
                self.command, f"http://localhost:{port}{self.path}", headers=dict(self.headers), content=body
            )
            try:
                response = state["fake"].handle(request)
            except httpx.TimeoutException:
                return self._send(504, b"", {})
            self._send(response.status_code, response.content, dict(response.headers))

        def _control(self, body: bytes) -> None:
            fake = state["fake"]
            if self.path == "/_fake/health":
                return self._send(200, b"ok", {})
            if self.path == "/_fake/reset":
                state["fake"] = standard()
                return self._send(204, b"", {})
            data = json.loads(body or b"{}")
            if self.path == "/_fake/edit":
                fake.edit(int(data["page_id"]), data["body"])
                return self._send(204, b"", {})
            if self.path == "/_fake/token":  # invalidate / restore a token at the "provider"
                fake.users[data["token"]].valid = bool(data.get("valid"))
                return self._send(204, b"", {})
            if self.path == "/_fake/space-access":
                user = fake.users[data["token"]]
                if data.get("grant"):
                    user.spaces[data["space"]] = {"read"}
                else:
                    user.spaces.pop(data["space"], None)
                return self._send(204, b"", {})
            return self._send(404, b"", {})

        do_GET = do_POST = do_DELETE = do_PATCH = do_PUT = _handle

        def log_message(self, *args: Any) -> None:  # keep test output quiet
            pass

    ThreadingHTTPServer(("127.0.0.1", port), Handler).serve_forever()


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="Fake Confluence API for local E2E tests")
    parser.add_argument("--port", type=int, default=18092)
    serve(parser.parse_args().port)
