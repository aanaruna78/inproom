"""Jira adapter (INPROOM-130 / story INPROOM-23): what schema discovery and the issue reader need.

Jira Cloud authenticates with email + API token (HTTP Basic) on REST v3; Data Center uses a personal access
token (Bearer) on REST v2. Both share the hardened HTTP core (timeouts, bounded retries, rate limits, no
redirects). Jira answers 403 for projects the credential cannot browse, so 403 is mapped to "no access".
This adapter only reads; write capability is discovered through `my_permissions`, never exercised.
"""

import base64
import time
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import quote

import httpx

from app.connectors.base import Account, ProviderError
from app.connectors.egress import validate_jira_url
from app.connectors.http import ProviderHttp
from app.connectors.vault import Secret

MAX_PAGES = 20  # hard cap when walking paginated lists
PAGE_SIZE = 50
READ_PERMISSION = "BROWSE_PROJECTS"
WRITE_PERMISSIONS = ("CREATE_ISSUES", "EDIT_ISSUES", "LINK_ISSUES", "TRANSITION_ISSUES", "ADD_COMMENTS")


@dataclass(frozen=True)
class JiraProject:
    id: str
    key: str
    name: str


@dataclass(frozen=True)
class IssueType:
    id: str
    name: str
    hierarchy_level: int  # 1 epic, 0 standard, -1 sub-task (Jira's own levels)
    subtask: bool


@dataclass(frozen=True)
class JiraField:
    id: str
    name: str
    custom: bool
    schema_type: str | None  # "number", "string", "array", "option", ...
    custom_type: str | None  # e.g. "com.pyxis.greenhopper.jira:gh-sprint"


@dataclass(frozen=True)
class Status:
    id: str
    name: str
    category: str  # Jira status category key: "new" | "indeterminate" | "done"


@dataclass(frozen=True)
class LinkType:
    id: str
    name: str
    inward: str
    outward: str


@dataclass(frozen=True)
class Board:
    id: str
    name: str
    type: str  # "scrum" | "kanban" | "simple"


@dataclass(frozen=True)
class Sprint:
    id: str
    name: str
    state: str  # "future" | "active" | "closed"
    board_id: str
    start: str | None = None
    end: str | None = None


@dataclass(frozen=True)
class IssuePage:
    issues: list[dict[str, Any]]  # raw {id, key, fields}; normalisation lives in the discovery/reader layer
    next_page_token: str | None
    extra: dict[str, Any] = field(default_factory=dict)


class JiraProvider:
    def __init__(
        self,
        base_url: str,
        token: Secret,
        *,
        email: str | None = None,
        transport: httpx.BaseTransport | None = None,
        max_attempts: int = 3,
        sleep: Callable[[float], None] = time.sleep,
    ):
        self.base_url = validate_jira_url(base_url)
        self.cloud = email is not None
        self.api = "/rest/api/3" if self.cloud else "/rest/api/2"
        if self.cloud:
            basic = base64.b64encode(f"{email}:{token.reveal()}".encode()).decode()
            auth = f"Basic {basic}"
        else:
            auth = f"Bearer {token.reveal()}"
        self._http = ProviderHttp(
            "Jira",
            self.base_url,
            {"Authorization": auth, "Accept": "application/json"},
            transport=transport,
            max_attempts=max_attempts,
            sleep=sleep,
            forbidden_is_no_access=True,
        )

    def close(self) -> None:
        self._http.close()

    def _get(self, path: str, **params: Any) -> Any:
        return self._http.request(
            "GET", path, params={k: v for k, v in params.items() if v is not None}
        ).json()

    def _offset_pages(self, path: str, params: dict[str, Any], start_at: int = 0, limit: int = MAX_PAGES):
        """Walk Jira's startAt/maxResults/isLast pagination (project search, boards, sprints)."""
        for _ in range(limit):
            data = self._get(path, **params, startAt=start_at, maxResults=PAGE_SIZE)
            values = data.get("values", [])
            last = data.get("isLast", start_at + len(values) >= data.get("total", 0))
            yield values, not last
            if last or not values:
                return
            start_at += len(values)

    # ------------------------------------------------------------ identity and projects

    def whoami(self) -> Account:
        data = self._get(f"{self.api}/myself")
        login = data.get("emailAddress") or data.get("name") or data.get("displayName") or "unknown"
        return Account(login=login, id=str(data.get("accountId") or data.get("key") or login), scopes=[])

    @staticmethod
    def _project(data: dict[str, Any]) -> JiraProject:
        return JiraProject(id=str(data["id"]), key=data["key"], name=data.get("name") or data["key"])

    def list_projects(self, q: str | None = None, page: int = 1) -> tuple[list[JiraProject], bool]:
        if self.cloud:
            pages = self._offset_pages(
                f"{self.api}/project/search", {"query": q, "orderBy": "key"}, (page - 1) * PAGE_SIZE, 1
            )
            values, more = next(pages)
            return [self._project(p) for p in values], more
        # Data Center has no paged project search: filter the full list.
        everything = [self._project(p) for p in self._get(f"{self.api}/project")]
        if q:
            everything = [p for p in everything if q.lower() in f"{p.key} {p.name}".lower()]
        chunk = everything[(page - 1) * PAGE_SIZE : page * PAGE_SIZE]
        return chunk, len(everything) > page * PAGE_SIZE

    def get_project(self, key_or_id: str) -> tuple[JiraProject, list[IssueType]]:
        data = self._get(f"{self.api}/project/{quote(key_or_id, safe='')}")
        types = [
            IssueType(
                id=str(t["id"]),
                name=t["name"],
                hierarchy_level=int(t.get("hierarchyLevel", -1 if t.get("subtask") else 0)),
                subtask=bool(t.get("subtask")),
            )
            for t in data.get("issueTypes", [])
        ]
        return self._project(data), types

    # ------------------------------------------------------------ schema

    def fields(self) -> list[JiraField]:
        out = []
        for f in self._get(f"{self.api}/field"):
            schema = f.get("schema") or {}
            out.append(
                JiraField(
                    id=f["id"],
                    name=f.get("name") or f["id"],
                    custom=bool(f.get("custom")),
                    schema_type=schema.get("type"),
                    custom_type=schema.get("custom"),
                )
            )
        return out

    def statuses(self, key: str) -> dict[str, list[Status]]:
        """Statuses per issue type id, with Jira's status category."""
        result: dict[str, list[Status]] = {}
        for issue_type in self._get(f"{self.api}/project/{quote(key, safe='')}/statuses"):
            result[str(issue_type["id"])] = [
                Status(
                    id=str(s["id"]),
                    name=s["name"],
                    category=(s.get("statusCategory") or {}).get("key", "indeterminate"),
                )
                for s in issue_type.get("statuses", [])
            ]
        return result

    def link_types(self) -> list[LinkType]:
        return [
            LinkType(
                id=str(t["id"]), name=t["name"], inward=t.get("inward", ""), outward=t.get("outward", "")
            )
            for t in self._get(f"{self.api}/issueLinkType").get("issueLinkTypes", [])
        ]

    def my_permissions(self, key: str) -> dict[str, bool]:
        wanted = (READ_PERMISSION, *WRITE_PERMISSIONS)
        data = self._get(f"{self.api}/mypermissions", projectKey=key, permissions=",".join(wanted))
        perms = data.get("permissions") or {}
        return {p: bool((perms.get(p) or {}).get("havePermission")) for p in wanted}

    def boards(self, key: str) -> list[Board]:
        return [
            Board(id=str(b["id"]), name=b.get("name") or str(b["id"]), type=b.get("type", "simple"))
            for values, _ in self._offset_pages("/rest/agile/1.0/board", {"projectKeyOrId": key})
            for b in values
        ]

    def sprints(self, board: Board) -> list[Sprint]:
        if board.type != "scrum":
            return []  # kanban/simple boards answer 400 "does not support sprints"
        return [
            Sprint(
                id=str(s["id"]),
                name=s.get("name") or str(s["id"]),
                state=s.get("state", "future"),
                board_id=board.id,
                start=s.get("startDate"),
                end=s.get("endDate"),
            )
            for values, _ in self._offset_pages(f"/rest/agile/1.0/board/{int(board.id)}/sprint", {})
            for s in values
        ]

    # ------------------------------------------------------------ issues

    def search_issues(
        self, jql: str, fields: list[str], page_token: str | None = None, max_results: int = PAGE_SIZE
    ) -> IssuePage:
        if self.cloud:
            body: dict[str, Any] = {"jql": jql, "fields": fields, "maxResults": max_results}
            if page_token:
                body["nextPageToken"] = page_token
            data = self._http.request("POST", f"{self.api}/search/jql", json=body).json()
            token = None if data.get("isLast", True) else data.get("nextPageToken")
            return IssuePage(issues=data.get("issues", []), next_page_token=token)
        start = int(page_token or 0)
        data = self._http.request(
            "POST",
            f"{self.api}/search",
            json={"jql": jql, "fields": fields, "startAt": start, "maxResults": max_results},
        ).json()
        issues = data.get("issues", [])
        more = start + len(issues) < data.get("total", 0) and bool(issues)
        return IssuePage(issues=issues, next_page_token=str(start + len(issues)) if more else None)

    def iter_issues(self, jql: str, fields: list[str], limit: int = MAX_PAGES) -> Iterator[dict[str, Any]]:
        token = None
        for _ in range(limit):
            page = self.search_issues(jql, fields, token)
            yield from page.issues
            if not page.next_page_token:
                return
            token = page.next_page_token
        raise ProviderError(f"Jira search exceeded {limit} pages; narrow the query")
