"""A small fake of the Jira Cloud / Data Center REST APIs (INPROOM-130).

In-process via `transport()` (httpx.MockTransport) for API tests, or as a real HTTP server via
`python -m tests.fakes.jira --port 18091` for browser E2E. State is plain data so tests can revoke access,
change the schema and inject failures. REST v3 (Cloud, Basic email:token) and v2 (Data Center, Bearer PAT)
are served from the same state.
"""

import base64
import copy
import json
import re
from dataclasses import dataclass, field
from typing import Any

import httpx

ALL_PERMS = {
    "BROWSE_PROJECTS",
    "CREATE_ISSUES",
    "EDIT_ISSUES",
    "LINK_ISSUES",
    "TRANSITION_ISSUES",
    "ADD_COMMENTS",
}
BROWSE_ONLY = {"BROWSE_PROJECTS"}

# Fake credentials (shaped like Atlassian API tokens; never real).
EMAIL = "bot@acme.example"
VALID_TOKEN = "ATATT3xFfGF0VALID00000000000000000000000000000000000000000000000000000000=AB12CD34"
READONLY_TOKEN = "ATATT3xFfGF0READONLY000000000000000000000000000000000000000000000000000=EF56AB78"
BAD_TOKEN = "ATATT3xFfGF0NOPE00000000000000000000000000000000000000000000000000000000=00000000"
DC_PAT = "NjQ2MzA0ODcwNzYzOkFAKEDATACENTERPATaBcDeFgHiJkLmNoPqRsTuVwXyZ012"

STORY_POINTS = "customfield_10016"
SPRINT = "customfield_10020"
EPIC_LINK = "customfield_10014"
ACCEPTANCE = "customfield_10050"
TEAM = "customfield_10060"


def _status(sid: str, name: str, category: str) -> dict[str, Any]:
    return {"id": sid, "name": name, "statusCategory": {"key": category}}


TODO = _status("1", "To Do", "new")
DOING = _status("3", "In Progress", "indeterminate")
REVIEW = _status("10001", "In Review", "indeterminate")
DONE = _status("10002", "Done", "done")
VERIFIED = _status("10003", "Verified", "done")


@dataclass
class FakeProject:
    id: str
    key: str
    name: str
    issue_types: list[dict[str, Any]]
    statuses: dict[str, list[dict[str, Any]]]  # issue type id -> statuses
    boards: list[dict[str, Any]] = field(default_factory=list)
    issues: list[dict[str, Any]] = field(default_factory=list)


@dataclass
class FakeUser:
    email: str
    account_id: str
    access: dict[str, set[str]]  # project key -> granted permissions
    valid: bool = True


class FakeJira:
    def __init__(self) -> None:
        self.projects: dict[str, FakeProject] = {}
        self.fields: list[dict[str, Any]] = []
        self.link_types: list[dict[str, Any]] = []
        self.sprints: dict[int, list[dict[str, Any]]] = {}  # board id -> sprints
        self.users: dict[str, FakeUser] = {}  # token -> user
        self.failures: list[str] = []  # next responses: "timeout" | "500" | "ratelimit" | "redirect"
        self.search_calls = 0
        self.fail_searches_from: int | None = None  # search call number from which searches answer 503
        self.calls: list[tuple[str, str]] = []

    # ---- setup helpers
    def add_user(self, token: str, email: str, access: dict[str, set[str]]) -> FakeUser:
        self.users[token] = FakeUser(
            email, f"acc-{len(self.users) + 1}", {k: set(v) for k, v in access.items()}
        )
        return self.users[token]

    def revoke(self, token: str, key: str) -> None:
        self.users[token].access.pop(key, None)

    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self.handle)

    # ---- request handling
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
            return user if user and user.email == email and user.valid else None
        user = self.users.get(auth.removeprefix("Bearer "))
        return user if user and user.valid else None

    @staticmethod
    def _page(values: list[Any], params: httpx.QueryParams) -> dict[str, Any]:
        start, size = int(params.get("startAt", "0")), int(params.get("maxResults", "50"))
        chunk = values[start : start + size]
        return {
            "values": chunk,
            "startAt": start,
            "maxResults": size,
            "total": len(values),
            "isLast": start + len(chunk) >= len(values),
        }

    def handle(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        self.calls.append((request.method, path))
        if self.failures:
            failure = self.failures.pop(0)
            if failure == "timeout":
                raise httpx.ReadTimeout("fake timeout", request=request)
            if failure == "500":
                return self._json(503, {"errorMessages": ["unavailable"]})
            if failure == "ratelimit":
                return self._json(429, {"errorMessages": ["rate limited"]}, {"retry-after": "42"})
            if failure == "redirect":
                return httpx.Response(302, headers={"location": "http://169.254.169.254/latest/meta-data"})
        user = self._user(request)
        if user is None:
            return self._json(401, {"errorMessages": ["Unauthorized"]})
        browsable = {k: p for k, p in self.projects.items() if "BROWSE_PROJECTS" in user.access.get(k, set())}
        params = request.url.params

        api = re.fullmatch(r"/rest/api/[23](/.*)", path)
        if api:
            return self._api(request, api.group(1), user, browsable, params)
        if path == "/rest/agile/1.0/board":
            key = params.get("projectKeyOrId", "")
            if key and key not in browsable:
                return self._json(404, {"errorMessages": ["project not found"]})
            boards = [b for k, p in browsable.items() if not key or k == key for b in p.boards]
            return self._json(200, self._page(boards, params))
        m = re.fullmatch(r"/rest/agile/1.0/board/(\d+)/sprint", path)
        if m:
            board_id = int(m.group(1))
            board = next((b for p in browsable.values() for b in p.boards if b["id"] == board_id), None)
            if board is None:
                return self._json(404, {"errorMessages": ["board not found"]})
            if board["type"] != "scrum":
                return self._json(400, {"errorMessages": ["The board does not support sprints"]})
            return self._json(200, self._page(self.sprints.get(board_id, []), params))
        return self._json(404, {"errorMessages": ["not found"]})

    def _api(
        self,
        request: httpx.Request,
        rest: str,
        user: FakeUser,
        browsable: dict[str, FakeProject],
        params: httpx.QueryParams,
    ) -> httpx.Response:
        if rest == "/myself":
            return self._json(
                200, {"accountId": user.account_id, "emailAddress": user.email, "displayName": "Bot"}
            )
        if rest == "/project/search":
            q = params.get("query", "").lower()
            found = [
                {"id": p.id, "key": p.key, "name": p.name}
                for p in sorted(browsable.values(), key=lambda p: p.key)
                if q in f"{p.key} {p.name}".lower()
            ]
            return self._json(200, self._page(found, params))
        if rest == "/project":
            return self._json(200, [{"id": p.id, "key": p.key, "name": p.name} for p in browsable.values()])
        m = re.fullmatch(r"/project/([^/]+)(/statuses)?", rest)
        if m:
            project = browsable.get(m.group(1)) or next(
                (p for p in browsable.values() if p.id == m.group(1)), None
            )
            if project is None:
                # Real Jira: 404 for unknown projects, 403-ish/404 for ones the user cannot browse.
                return self._json(404, {"errorMessages": ["No project could be found"]})
            if m.group(2):
                return self._json(
                    200,
                    [
                        {"id": t["id"], "name": t["name"], "statuses": project.statuses.get(t["id"], [])}
                        for t in project.issue_types
                    ],
                )
            return self._json(
                200,
                {
                    "id": project.id,
                    "key": project.key,
                    "name": project.name,
                    "issueTypes": project.issue_types,
                },
            )
        if rest == "/field":
            return self._json(200, self.fields)
        if rest == "/issueLinkType":
            return self._json(200, {"issueLinkTypes": self.link_types})
        if rest == "/mypermissions":
            key = params.get("projectKey", "")
            if key not in self.projects:
                return self._json(404, {"errorMessages": ["No project could be found"]})
            granted = user.access.get(key, set())
            return self._json(
                200,
                {
                    "permissions": {
                        p: {"key": p, "havePermission": p in granted}
                        for p in params.get("permissions", "").split(",")
                    }
                },
            )
        if rest in ("/search/jql", "/search") and request.method == "POST":
            return self._search(rest, json.loads(request.content or b"{}"), browsable)
        return self._json(404, {"errorMessages": ["not found"]})

    def _search(self, rest: str, body: dict[str, Any], browsable: dict[str, FakeProject]) -> httpx.Response:
        self.search_calls += 1
        if self.fail_searches_from is not None and self.search_calls >= self.fail_searches_from:
            return self._json(503, {"errorMessages": ["search unavailable"]})  # an outage mid-scan
        jql = body.get("jql", "")
        m = re.search(r'project\s*=\s*"?([A-Za-z0-9_]+)"?', jql)
        project = m and next((p for p in browsable.values() if m.group(1) in (p.key, p.id)), None)
        if not project:
            return self._json(400, {"errorMessages": ["The value in the project clause does not exist"]})
        issues = project.issues
        since = re.search(r'updated\s*>=\s*"([^"]+)"', jql)
        if since:
            issues = [i for i in issues if i["fields"]["updated"] >= since.group(1)]
        wanted = body.get("fields") or ["*all"]
        size = int(body.get("maxResults", 50))
        if rest == "/search/jql":
            start = int(body.get("nextPageToken") or 0)
        else:
            start = int(body.get("startAt", 0))
        chunk = [
            {
                "id": i["id"],
                "key": i["key"],
                "fields": {k: v for k, v in i["fields"].items() if "*all" in wanted or k in wanted},
            }
            for i in issues[start : start + size]
        ]
        last = start + size >= len(issues)
        if rest == "/search/jql":
            out: dict[str, Any] = {"issues": chunk, "isLast": last}
            if not last:
                out["nextPageToken"] = str(start + size)
            return self._json(200, out)
        return self._json(200, {"issues": chunk, "startAt": start, "maxResults": size, "total": len(issues)})


# ---------------------------------------------------------------- the standard site


def _type(tid: str, name: str, level: int) -> dict[str, Any]:
    return {"id": tid, "name": name, "hierarchyLevel": level, "subtask": level < 0}


def _issue(
    iid: str,
    key: str,
    itype: dict[str, Any],
    status: dict[str, Any],
    summary: str,
    *,
    parent: str | None = None,
    points: float | None = None,
    sprints: list[int] | None = None,
    acceptance: str | None = None,
    links: list[dict[str, Any]] | None = None,
    updated: str = "2026-11-20T10:00:00.000+0000",
) -> dict[str, Any]:
    fields: dict[str, Any] = {
        "summary": summary,
        "created": "2026-01-05T09:00:00.000+0000",
        "creator": {"accountId": "acc-ada", "displayName": "Ada"},
        "reporter": {"accountId": "acc-grace", "displayName": "Grace"},
        "issuetype": {"id": itype["id"], "name": itype["name"]},
        "status": status,
        "labels": [],
        "issuelinks": links or [],
        "updated": updated,
        STORY_POINTS: points,
        SPRINT: [{"id": s, "name": f"Sprint {s}", "state": "active"} for s in (sprints or [])] or None,
        ACCEPTANCE: acceptance,
    }
    if parent:
        fields["parent"] = {"key": parent}
    return {"id": iid, "key": key, "fields": fields}


def _link(
    name: str, inward: str, outward: str, *, outward_key: str | None = None, inward_key: str | None = None
):
    link: dict[str, Any] = {"type": {"name": name, "inward": inward, "outward": outward}}
    if outward_key:
        link["outwardIssue"] = {"key": outward_key}
    if inward_key:
        link["inwardIssue"] = {"key": inward_key}
    return link


def standard() -> FakeJira:
    """ABC: full schema (epics, story points, sprints, acceptance criteria, links).
    NOEP: no epic type, no board.
    HID: exists but nobody can browse it. VALID_TOKEN can write ABC; READONLY_TOKEN can only browse."""
    fake = FakeJira()
    fake.fields = [
        {
            "id": "summary",
            "name": "Summary",
            "custom": False,
            "schema": {"type": "string", "system": "summary"},
        },
        {"id": "description", "name": "Description", "custom": False, "schema": {"type": "string"}},
        {"id": "parent", "name": "Parent", "custom": False, "schema": {"type": "issuelink"}},
        {"id": "labels", "name": "Labels", "custom": False, "schema": {"type": "array", "items": "string"}},
        {"id": "issuelinks", "name": "Linked Issues", "custom": False, "schema": {"type": "array"}},
        {
            "id": STORY_POINTS,
            "name": "Story point estimate",
            "custom": True,
            "schema": {"type": "number", "custom": "com.pyxis.greenhopper.jira:jsw-story-points"},
        },
        {
            "id": SPRINT,
            "name": "Sprint",
            "custom": True,
            "schema": {"type": "array", "items": "json", "custom": "com.pyxis.greenhopper.jira:gh-sprint"},
        },
        {
            "id": EPIC_LINK,
            "name": "Epic Link",
            "custom": True,
            "schema": {"type": "any", "custom": "com.pyxis.greenhopper.jira:gh-epic-link"},
        },
        {
            "id": ACCEPTANCE,
            "name": "Acceptance Criteria",
            "custom": True,
            "schema": {
                "type": "string",
                "custom": "com.atlassian.jira.plugin.system.customfieldtypes:textarea",
            },
        },
        {
            "id": TEAM,
            "name": "Team",
            "custom": True,
            "schema": {
                "type": "option",
                "custom": "com.atlassian.jira.plugin.system.customfieldtypes:select",
            },
        },
    ]
    fake.link_types = [
        {"id": "10000", "name": "Blocks", "inward": "is blocked by", "outward": "blocks"},
        {"id": "10003", "name": "Relates", "inward": "relates to", "outward": "relates to"},
    ]
    epic, story, task, sub, bug = (
        _type("10000", "Epic", 1),
        _type("10001", "Story", 0),
        _type("10002", "Task", 0),
        _type("10003", "Sub-task", -1),
        _type("10004", "Bug", 0),
    )
    flow = [TODO, DOING, REVIEW, DONE]
    abc = FakeProject(
        "10100",
        "ABC",
        "Acme Billing",
        [epic, story, task, sub, bug],
        {t["id"]: copy.deepcopy(flow) for t in (epic, story, task, sub)} | {bug["id"]: [*flow, VERIFIED]},
        boards=[
            {"id": 1, "name": "ABC board", "type": "scrum"},
            {"id": 2, "name": "ABC kanban", "type": "kanban"},
        ],
    )
    abc.issues = [
        _issue("20001", "ABC-1", epic, DOING, "Invoicing"),
        _issue(
            "20002",
            "ABC-2",
            story,
            DOING,
            "Create invoice",
            parent="ABC-1",
            points=5,
            sprints=[12],
            acceptance="Given a customer, when invoiced, then a PDF is produced",
            links=[_link("Blocks", "is blocked by", "blocks", outward_key="ABC-3")],
        ),
        _issue(
            "20003",
            "ABC-3",
            story,
            TODO,
            "Send invoice",
            parent="ABC-1",
            points=3,
            sprints=[12],
            links=[
                _link("Blocks", "is blocked by", "blocks", inward_key="ABC-2"),
                _link("Relates", "relates to", "relates to", outward_key="ABC-5"),
            ],
            updated="2026-11-22T09:00:00.000+0000",
        ),
        _issue("20004", "ABC-4", sub, DONE, "Template", parent="ABC-3"),
        _issue("20005", "ABC-5", bug, VERIFIED, "Rounding error", sprints=[11]),
    ]
    fake.sprints[1] = [
        {
            "id": 11,
            "name": "Sprint 11",
            "state": "closed",
            "startDate": "2026-11-01",
            "endDate": "2026-11-14",
        },
        {
            "id": 12,
            "name": "Sprint 12",
            "state": "active",
            "startDate": "2026-11-15",
            "endDate": "2026-11-28",
        },
    ]
    noep = FakeProject("10101", "NOEP", "No Epics", [task, sub], {task["id"]: flow, sub["id"]: flow})
    noep.issues = [_issue("20101", "NOEP-1", task, TODO, "Lonely task")]
    hid = FakeProject("10102", "HID", "Hidden", [task], {task["id"]: flow})
    for p in (abc, noep, hid):
        fake.projects[p.key] = p
    fake.add_user(VALID_TOKEN, EMAIL, {"ABC": ALL_PERMS, "NOEP": ALL_PERMS})
    fake.add_user(READONLY_TOKEN, "reader@acme.example", {"ABC": BROWSE_ONLY, "NOEP": BROWSE_ONLY})
    fake.add_user(DC_PAT, "dc-bot", {"ABC": BROWSE_ONLY})
    return fake


def serve(port: int) -> None:
    """Run the standard fake as a local HTTP server for browser E2E. NEVER expose it: test-only.

    Control endpoints (test-only): POST /_fake/reset, POST /_fake/access {"token","project","grant"},
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
            if self.path == "/_fake/health":
                return self._send(200, b"ok", {})
            if self.path == "/_fake/reset":
                state["fake"] = standard()
                return self._send(204, b"", {})
            if self.path == "/_fake/access":
                data = json.loads(body)
                user = state["fake"].users[data["token"]]
                if data.get("grant"):
                    user.access[data["project"]] = set(BROWSE_ONLY)
                else:
                    user.access.pop(data["project"], None)
                return self._send(204, b"", {})
            return self._send(404, b"", {})

        do_GET = do_POST = do_DELETE = do_PATCH = do_PUT = _handle

        def log_message(self, *args: Any) -> None:  # keep test output quiet
            pass

    ThreadingHTTPServer(("127.0.0.1", port), Handler).serve_forever()


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="Fake Jira API for local E2E tests")
    parser.add_argument("--port", type=int, default=18091)
    serve(parser.parse_args().port)


def legacy_project(
    fake: FakeJira, key: str = "LEG", pid: str = "10200", epics: int = 3, stories: int = 4, subtasks: int = 2
) -> FakeProject:
    """A long-lived project to migrate (INPROOM-151): epics -> stories -> sub-tasks, plus the awkward cases a
    real migration meets: an orphan (parent not visible), an unsupported type, a link to another project.
    Size = epics * (1 + stories * (1 + subtasks)) + 3. VALID_TOKEN can browse it."""
    epic, story, sub, spike = (
        _type("10000", "Epic", 1),
        _type("10001", "Story", 0),
        _type("10003", "Sub-task", -1),
        _type("10009", "Spike", 0),
    )
    flow = [TODO, DOING, REVIEW, DONE]
    project = FakeProject(
        pid,
        key,
        f"{key} legacy",
        [epic, story, sub, spike],
        {t["id"]: flow for t in (epic, story, sub, spike)},
    )
    seq = iter(range(1, 10**7))
    ident = iter(range(int(pid) * 1000, int(pid) * 1000 + 10**6))

    def add(
        itype: dict[str, Any], summary: str, parent: dict[str, Any] | None = None, **kw: Any
    ) -> dict[str, Any]:
        n, iid = next(seq), str(next(ident))
        issue = _issue(iid, f"{key}-{n}", itype, flow[n % 4], summary, **kw)
        if parent:
            issue["fields"]["parent"] = {"id": parent["id"], "key": parent["key"]}
        project.issues.append(issue)
        return issue

    for e in range(epics):
        ep = add(epic, f"Epic {e}")
        for s_ in range(stories):
            st = add(story, f"Story {e}.{s_}", ep)
            for t in range(subtasks):
                add(sub, f"Sub-task {e}.{s_}.{t}", st)
    orphan = add(story, "Orphan story")
    orphan["fields"]["parent"] = {"id": "99999999", "key": "OLD-1"}  # parent deleted / not visible
    add(spike, "Investigate caching")  # a type outside epic / story / task / sub-task / bug
    add(story, "Linked elsewhere", links=[_link("Blocks", "is blocked by", "blocks", outward_key="OTHER-7")])
    fake.projects[key] = project
    fake.users[VALID_TOKEN].access[key] = set(ALL_PERMS)
    return project
