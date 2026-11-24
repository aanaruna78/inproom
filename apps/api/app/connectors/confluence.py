"""Confluence adapter (INPROOM-134 / story INPROOM-24): spaces, pages, versions, restrictions, attachments.

Cloud authenticates with email + API token (HTTP Basic) under /wiki; Data Center with a personal access token
(Bearer) at the site root. Both expose the same content API (/rest/api), so one adapter serves both; paging
follows the `_links.next` the server returns (offset on DC, cursor-style on Cloud). Shares the hardened HTTP
core. Only reads: write capability is discovered from the space's operations, never exercised.
"""

import base64
import hashlib
import time
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import parse_qs, quote, urlsplit

import httpx

from app.connectors.base import Account, NotFoundOrNoAccess
from app.connectors.egress import validate_confluence_url
from app.connectors.http import ProviderHttp
from app.connectors.vault import Secret

PAGE_SIZE = 50
MAX_PAGES = 5000  # sync cap per run (reported as truncated)
MAX_LIST_PAGES = 20  # cap when walking space lists
PAGE_EXPAND = ",".join(
    (
        "version",
        "ancestors",
        "space",
        "body.storage",
        "metadata.labels",
        "children.attachment",
        "restrictions.read.restrictions.user",
        "restrictions.read.restrictions.group",
    )
)
WRITE_OPERATIONS = {"create", "update", "delete", "administer"}


@dataclass(frozen=True)
class Space:
    id: str
    key: str
    name: str
    write_capable: bool | None = None  # None = the server did not say


@dataclass(frozen=True)
class Attachment:
    id: str
    title: str
    media_type: str | None
    size: int | None
    version: int | None


@dataclass(frozen=True)
class PageMeta:
    id: str
    title: str
    space_key: str
    parent_id: str | None
    ancestors: list[str]
    version: int
    author: str | None
    created_at: str | None
    message: str | None
    labels: list[str]
    restrictions: dict[str, list[str]]  # {"users": [...], "groups": [...]} on this page itself
    attachments: list[Attachment]
    content_sha256: str
    web_url: str | None


@dataclass(frozen=True)
class PageBody:
    id: str
    title: str
    space_key: str
    version: int
    storage: str
    content_sha256: str


@dataclass(frozen=True)
class PageVersion:
    number: int
    author: str | None
    created_at: str | None
    message: str | None


@dataclass
class PageListing:
    pages: list[PageMeta] = field(default_factory=list)
    truncated: bool = False


def content_hash(storage: str) -> str:
    return hashlib.sha256(storage.encode()).hexdigest()


class ConfluenceProvider:
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
        self.base_url = validate_confluence_url(base_url)
        self.cloud = email is not None
        # Cloud serves Confluence under /wiki on the shared Atlassian site; Data Center at its own root.
        self.api = "/wiki/rest/api" if self.cloud else "/rest/api"
        if self.cloud:
            auth = "Basic " + base64.b64encode(f"{email}:{token.reveal()}".encode()).decode()
        else:
            auth = f"Bearer {token.reveal()}"
        self._http = ProviderHttp(
            "Confluence",
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

    def _walk(self, path: str, params: dict[str, Any], limit_pages: int) -> Iterator[tuple[list[dict], bool]]:
        """Follow `_links.next`. Only its query string is reused, so the server can never redirect us
        to another host or path (SSRF) through a crafted next link."""
        query = dict(params)
        for _ in range(limit_pages):
            data = self._get(path, **query)
            nxt = (data.get("_links") or {}).get("next")
            yield data.get("results", []), bool(nxt)
            if not nxt:
                return
            query = {k: v[-1] for k, v in parse_qs(urlsplit(nxt).query).items()}

    # ------------------------------------------------------------ identity and spaces

    def whoami(self) -> Account:
        data = self._get(f"{self.api}/user/current")
        login = data.get("email") or data.get("username") or data.get("displayName") or "unknown"
        return Account(login=login, id=str(data.get("accountId") or data.get("userKey") or login), scopes=[])

    @staticmethod
    def _space(data: dict[str, Any]) -> Space:
        ops = data.get("operations")
        write = None if ops is None else any(o.get("operation") in WRITE_OPERATIONS for o in ops)
        return Space(
            id=str(data["id"]), key=data["key"], name=data.get("name") or data["key"], write_capable=write
        )

    def list_spaces(self, q: str | None = None, start: int = 0) -> tuple[list[Space], int | None]:
        """One page of spaces (or, with q, every matching space within the walk cap)."""
        params = {"limit": PAGE_SIZE, "start": start, "type": "global", "status": "current"}
        if not q:
            values, more = next(self._walk(f"{self.api}/space", params, 1))
            return [self._space(s) for s in values], (start + len(values)) if more else None
        needle = q.lower()
        found = [
            self._space(s)
            for values, _ in self._walk(f"{self.api}/space", params, MAX_LIST_PAGES)
            for s in values
            if needle in f"{s['key']} {s.get('name', '')}".lower()
        ]
        return found, None

    def get_space(self, key: str) -> Space:
        return self._space(self._get(f"{self.api}/space/{quote(key, safe='')}", expand="operations"))

    # ------------------------------------------------------------ pages

    @staticmethod
    def _restrictions(data: dict[str, Any]) -> dict[str, list[str]]:
        read = ((data.get("restrictions") or {}).get("read") or {}).get("restrictions") or {}
        users = [
            u.get("accountId") or u.get("username") or u.get("displayName", "?")
            for u in (read.get("user") or {}).get("results", [])
        ]
        groups = [g.get("name", "?") for g in (read.get("group") or {}).get("results", [])]
        return {"users": sorted(users), "groups": sorted(groups)}

    @staticmethod
    def _attachments(data: dict[str, Any]) -> list[Attachment]:
        results = (((data.get("children") or {}).get("attachment")) or {}).get("results", [])
        return [
            Attachment(
                id=str(a["id"]),
                title=a.get("title", ""),
                media_type=(a.get("metadata") or {}).get("mediaType"),
                size=(a.get("extensions") or {}).get("fileSize"),
                version=(a.get("version") or {}).get("number"),
            )
            for a in results
        ]

    def _meta(self, data: dict[str, Any]) -> PageMeta:
        version = data.get("version") or {}
        ancestors = [str(a["id"]) for a in data.get("ancestors") or []]
        storage = ((data.get("body") or {}).get("storage") or {}).get("value", "")
        labels = (((data.get("metadata") or {}).get("labels")) or {}).get("results", [])
        webui = (data.get("_links") or {}).get("webui")
        return PageMeta(
            id=str(data["id"]),
            title=data.get("title", ""),
            space_key=(data.get("space") or {}).get("key", ""),
            parent_id=ancestors[-1] if ancestors else None,
            ancestors=ancestors,
            version=int(version.get("number", 1)),
            author=(version.get("by") or {}).get("displayName"),
            created_at=version.get("when"),
            message=version.get("message") or None,
            labels=sorted(lbl["name"] for lbl in labels if lbl.get("name")),
            restrictions=self._restrictions(data),
            attachments=self._attachments(data),
            content_sha256=content_hash(storage),
            web_url=webui,
        )

    def iter_pages(self, space_key: str, cap: int = MAX_PAGES) -> PageListing:
        """Every current page of the space (drafts, archived and trashed pages are never requested)."""
        listing = PageListing()
        params = {
            "spaceKey": space_key,
            "type": "page",
            "status": "current",
            "limit": PAGE_SIZE,
            "expand": PAGE_EXPAND,
        }
        for values, more in self._walk(f"{self.api}/content", params, cap // PAGE_SIZE + 1):
            for item in values:
                if len(listing.pages) >= cap:
                    listing.truncated = True
                    return listing
                if item.get("status", "current") == "current":
                    listing.pages.append(self._meta(item))
            if not more:
                return listing
        listing.truncated = True
        return listing

    def get_page(self, page_id: str, version: int | None = None) -> PageBody:
        params: dict[str, Any] = {"expand": "body.storage,version,space"}
        if version is not None:
            params |= {"status": "historical", "version": version}
        data = self._get(f"{self.api}/content/{int(page_id)}", **params)
        storage = ((data.get("body") or {}).get("storage") or {}).get("value", "")
        return PageBody(
            id=str(data["id"]),
            title=data.get("title", ""),
            space_key=(data.get("space") or {}).get("key", ""),
            version=int((data.get("version") or {}).get("number", 1)),
            storage=storage,
            content_sha256=content_hash(storage),
        )

    def page_versions(self, page_id: str) -> list[PageVersion]:
        out = []
        for values, _ in self._walk(f"{self.api}/content/{int(page_id)}/version", {"limit": PAGE_SIZE}, 10):
            out += [
                PageVersion(
                    number=int(v["number"]),
                    author=(v.get("by") or {}).get("displayName"),
                    created_at=v.get("when"),
                    message=v.get("message") or None,
                )
                for v in values
            ]
        return sorted(out, key=lambda v: v.number)

    def can_read_space(self, key: str) -> bool:
        try:
            self.get_space(key)
            return True
        except NotFoundOrNoAccess:
            return False
