"""Confluence adapter (INPROOM-134 / story INPROOM-24): spaces, pages, versions, restrictions, attachments.

Cloud (email + API token, HTTP Basic, under /wiki) uses the REST v2 API with cursor paging (INPROOM-138):
  /wiki/api/v2/spaces, /spaces/{id}/operations, /spaces/{id}/pages, /pages/{id}[/labels|/attachments|
  /versions|/ancestors].
  Two calls stay on v1 because v2 has no equivalent: the current user (/wiki/rest/api/user/current) and page
  read restrictions (/wiki/rest/api/content/{id}/restriction/byOperation/read).
Data Center (personal access token, Bearer, at the site root) uses the v1 content API (/rest/api).
Paging always follows the `_links.next` the server returns, reusing only its query string (no SSRF steering).
Shares the hardened HTTP core. Only reads: write capability comes from the space's operations, never used.
"""

import base64
import hashlib
import re
import time
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import parse_qs, quote, urlsplit

import httpx

from app.connectors.base import Account, NotFoundOrNoAccess, ProviderError
from app.connectors.egress import validate_confluence_url
from app.connectors.http import ProviderHttp, download
from app.connectors.vault import Secret

PAGE_SIZE = 50
MAX_PAGES = 5000  # sync cap per run (reported as truncated)
MAX_LIST_PAGES = 20  # cap when walking space lists
V1_PAGE_EXPAND = ",".join(
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
    storage: str = field(default="", repr=False, compare=False)  # the body, kept as stored (INPROOM-158)


@dataclass(frozen=True)
class PageBody:
    id: str
    title: str
    space_id: str
    status: str
    version: int
    storage: str
    content_sha256: str


@dataclass(frozen=True)
class PageVersion:
    number: int
    author: str | None
    created_at: str | None
    message: str | None


@dataclass(frozen=True)
class LivePage:
    """A page's current eligibility facts, read live (INPROOM-139) to fail closed before serving content."""

    id: str
    status: str
    space_id: str
    labels: list[str]
    ancestors: list[str]
    restrictions: dict[str, list[str]]  # own read restrictions
    inherited: list[dict[str, Any]]  # [{"page_id", "users", "groups"}] from restricted ancestors

    @property
    def restricted(self) -> bool:
        own = self.restrictions["users"] or self.restrictions["groups"]
        return bool(own or self.inherited)


@dataclass
class PageListing:
    pages: list[PageMeta] = field(default_factory=list)
    truncated: bool = False
    # (page_id, attachment_id, max_bytes) -> bytes, bound to the provider that listed (INPROOM-166)
    downloader: Callable[[str, str, int], bytes] | None = field(default=None, repr=False, compare=False)


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
        self.v1 = "/wiki/rest/api" if self.cloud else "/rest/api"
        self.v2 = "/wiki/api/v2"
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

    def _walk(
        self, path: str, params: dict[str, Any], limit_pages: int
    ) -> Iterator[tuple[list[dict], str | None]]:
        """Follow `_links.next`, yielding (results, next query). Only the next link's query string is reused,
        so the server can never steer a request to another host or path (SSRF) through a crafted link."""
        query = dict(params)
        for _ in range(limit_pages):
            data = self._get(path, **query)
            nxt = (data.get("_links") or {}).get("next")
            query = {k: v[-1] for k, v in parse_qs(urlsplit(nxt).query).items()} if nxt else {}
            yield data.get("results", []), (urlsplit(nxt).query if nxt else None)
            if not nxt:
                return

    def _all(self, path: str, params: dict[str, Any], limit_pages: int = 20) -> list[dict]:
        return [item for values, _ in self._walk(path, params, limit_pages) for item in values]

    # ------------------------------------------------------------ identity and spaces

    def whoami(self) -> Account:
        data = self._get(f"{self.v1}/user/current")  # v1 on Cloud too: v2 has no current-user endpoint
        login = data.get("email") or data.get("username") or data.get("displayName") or "unknown"
        return Account(login=login, id=str(data.get("accountId") or data.get("userKey") or login), scopes=[])

    @staticmethod
    def _write(ops: list[dict[str, Any]] | None) -> bool | None:
        return None if ops is None else any(o.get("operation") in WRITE_OPERATIONS for o in ops)

    def list_spaces(self, q: str | None = None, cursor: str | None = None) -> tuple[list[Space], str | None]:
        """One page of spaces and an opaque cursor for the next (or, with q, every match within the cap)."""
        if self.cloud:
            path, params = f"{self.v2}/spaces", {"limit": PAGE_SIZE, "type": "global", "status": "current"}
        else:
            path, params = f"{self.v1}/space", {"limit": PAGE_SIZE, "type": "global", "status": "current"}
        if cursor:
            params |= {
                k: v[-1]
                for k, v in parse_qs(
                    base64.urlsafe_b64decode(cursor + "=" * (-len(cursor) % 4)).decode()
                ).items()
            }

        def space(s: dict[str, Any]) -> Space:
            return Space(id=str(s["id"]), key=s["key"], name=s.get("name") or s["key"])

        if not q:
            values, nxt = next(self._walk(path, params, 1))
            token = base64.urlsafe_b64encode(nxt.encode()).decode().rstrip("=") if nxt else None
            return [space(s) for s in values], token
        needle = q.lower()
        return [
            space(s)
            for s in self._all(path, params, MAX_LIST_PAGES)
            if needle in f"{s['key']} {s.get('name', '')}".lower()
        ], None

    def get_space(self, key: str) -> Space:
        if self.cloud:
            found = self._get(f"{self.v2}/spaces", keys=key, limit=1).get("results", [])
            if not found:
                raise NotFoundOrNoAccess("space not found, or the credential cannot read it")
            data = found[0]
            ops = self._get(f"{self.v2}/spaces/{int(data['id'])}/operations").get("operations")
        else:
            data = self._get(f"{self.v1}/space/{quote(key, safe='')}", expand="operations")
            ops = data.get("operations")
        return Space(
            id=str(data["id"]),
            key=data["key"],
            name=data.get("name") or data["key"],
            write_capable=self._write(ops),
        )

    # ------------------------------------------------------------ restrictions and metadata

    @staticmethod
    def _acl(read: dict[str, Any]) -> dict[str, list[str]]:
        users = [
            u.get("accountId") or u.get("username") or u.get("displayName", "?")
            for u in (read.get("user") or {}).get("results", [])
        ]
        groups = [g.get("name", "?") for g in (read.get("group") or {}).get("results", [])]
        return {"users": sorted(users), "groups": sorted(groups)}

    def restrictions(self, page_id: str) -> dict[str, list[str]]:
        """A page's own read restrictions. v1 on Cloud too: v2 has no content-restrictions endpoint."""
        data = self._get(f"{self.v1}/content/{int(page_id)}/restriction/byOperation/read")
        return self._acl(data.get("restrictions") or {})

    def _v2_attachments(self, page_id: str) -> list[Attachment]:
        return [
            Attachment(
                id=str(a["id"]),
                title=a.get("title", ""),
                media_type=a.get("mediaType"),
                size=a.get("fileSize"),
                version=(a.get("version") or {}).get("number"),
            )
            for a in self._all(f"{self.v2}/pages/{int(page_id)}/attachments", {"limit": PAGE_SIZE}, 5)
        ]

    def download_attachment(self, page_id: str, attachment_id: str, max_bytes: int) -> bytes:
        """An attachment's bytes (INPROOM-166), via the fixed REST download path only: never a URL taken from
        a provider response. Cloud redirects to its media service; that hop must be allowlisted."""
        if not re.fullmatch(r"[A-Za-z0-9]{1,64}", attachment_id):
            raise ProviderError("invalid attachment id")
        path = f"{self.v1}/content/{int(page_id)}/child/attachment/{attachment_id}/download"
        return download(self._http, path, max_bytes, self._redirect_allowed)

    def _redirect_allowed(self, url: str) -> bool:
        from app.config import get_settings
        from app.connectors.egress import EgressDenied, validate_base_url

        parts = urlsplit(url)
        if parts.username or parts.password:
            return False
        origin = f"{parts.scheme}://{parts.netloc}"
        if origin == self.base_url.split("/wiki")[0].rstrip("/") or url.startswith(self.base_url + "/"):
            return True
        try:
            validate_base_url(origin, get_settings().confluence_attachment_hosts, "attachment host")
        except EgressDenied:
            return False
        return True

    def _v2_labels(self, page_id: str) -> list[str]:
        return sorted(
            lbl["name"]
            for lbl in self._all(f"{self.v2}/pages/{int(page_id)}/labels", {"limit": PAGE_SIZE}, 5)
        )

    # ------------------------------------------------------------ pages

    def iter_pages(self, space: Space, cap: int = MAX_PAGES) -> PageListing:
        """Every current page of the space (drafts, archived and trashed pages are never requested)."""
        listing = self._v2_pages(space, cap) if self.cloud else self._v1_pages(space, cap)
        listing.downloader = self.download_attachment
        return listing

    def _v2_pages(self, space: Space, cap: int) -> PageListing:
        listing = PageListing()
        raw: list[dict[str, Any]] = []
        params = {"status": "current", "body-format": "storage", "limit": PAGE_SIZE}
        for values, nxt in self._walk(
            f"{self.v2}/spaces/{int(space.id)}/pages", params, cap // PAGE_SIZE + 1
        ):
            for item in values:
                if len(raw) >= cap:
                    listing.truncated = True
                    break
                if item.get("status", "current") == "current":
                    raw.append(item)
            if listing.truncated or not nxt:
                break
        else:
            listing.truncated = True
        parents = {
            str(p["id"]): (
                str(p["parentId"]) if p.get("parentId") and p.get("parentType", "page") == "page" else None
            )
            for p in raw
        }
        for item in raw:
            pid = str(item["id"])
            chain, cur = [], parents.get(pid)
            while cur and cur not in chain:
                chain.insert(0, cur)
                cur = parents.get(cur)
            version = item.get("version") or {}
            storage = ((item.get("body") or {}).get("storage") or {}).get("value", "")
            listing.pages.append(
                PageMeta(
                    id=pid,
                    title=item.get("title", ""),
                    space_key=space.key,
                    parent_id=parents.get(pid),
                    ancestors=chain,
                    version=int(version.get("number", 1)),
                    author=version.get("authorId"),
                    created_at=version.get("createdAt"),
                    message=version.get("message") or None,
                    labels=self._v2_labels(pid),
                    restrictions=self.restrictions(pid),
                    attachments=self._v2_attachments(pid),
                    content_sha256=content_hash(storage),
                    web_url=(item.get("_links") or {}).get("webui"),
                    storage=storage,
                )
            )
        return listing

    def _v1_meta(self, data: dict[str, Any]) -> PageMeta:
        version = data.get("version") or {}
        ancestors = [str(a["id"]) for a in data.get("ancestors") or []]
        storage = ((data.get("body") or {}).get("storage") or {}).get("value", "")
        labels = (((data.get("metadata") or {}).get("labels")) or {}).get("results", [])
        read = ((data.get("restrictions") or {}).get("read") or {}).get("restrictions") or {}
        attachments = (((data.get("children") or {}).get("attachment")) or {}).get("results", [])
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
            restrictions=self._acl(read),
            attachments=[
                Attachment(
                    id=str(a["id"]),
                    title=a.get("title", ""),
                    media_type=(a.get("metadata") or {}).get("mediaType"),
                    size=(a.get("extensions") or {}).get("fileSize"),
                    version=(a.get("version") or {}).get("number"),
                )
                for a in attachments
            ],
            content_sha256=content_hash(storage),
            web_url=(data.get("_links") or {}).get("webui"),
            storage=storage,
        )

    def _v1_pages(self, space: Space, cap: int) -> PageListing:
        listing = PageListing()
        params = {
            "spaceKey": space.key,
            "type": "page",
            "status": "current",
            "limit": PAGE_SIZE,
            "expand": V1_PAGE_EXPAND,
        }
        for values, nxt in self._walk(f"{self.v1}/content", params, cap // PAGE_SIZE + 1):
            for item in values:
                if len(listing.pages) >= cap:
                    listing.truncated = True
                    return listing
                if item.get("status", "current") == "current":
                    listing.pages.append(self._v1_meta(item))
            if not nxt:
                return listing
        listing.truncated = True
        return listing

    def get_page(self, page_id: str, version: int | None = None) -> PageBody:
        if self.cloud:
            data = self._get(
                f"{self.v2}/pages/{int(page_id)}", **{"body-format": "storage", "version": version}
            )
            space_id = str(data.get("spaceId", ""))
        else:
            params: dict[str, Any] = {"expand": "body.storage,version,space"}
            if version is not None:
                params |= {"status": "historical", "version": version}
            data = self._get(f"{self.v1}/content/{int(page_id)}", **params)
            space_id = str((data.get("space") or {}).get("id", ""))
        storage = ((data.get("body") or {}).get("storage") or {}).get("value", "")
        return PageBody(
            id=str(data["id"]),
            title=data.get("title", ""),
            space_id=space_id,
            status=data.get("status", "current"),
            version=int((data.get("version") or {}).get("number", 1)),
            storage=storage,
            content_sha256=content_hash(storage),
        )

    def page_versions(self, page_id: str) -> list[PageVersion]:
        if self.cloud:
            items = self._all(f"{self.v2}/pages/{int(page_id)}/versions", {"limit": PAGE_SIZE}, 10)
            out = [
                PageVersion(
                    number=int(v["number"]),
                    author=v.get("authorId"),
                    created_at=v.get("createdAt"),
                    message=v.get("message") or None,
                )
                for v in items
            ]
        else:
            items = self._all(f"{self.v1}/content/{int(page_id)}/version", {"limit": PAGE_SIZE}, 10)
            out = [
                PageVersion(
                    number=int(v["number"]),
                    author=(v.get("by") or {}).get("displayName"),
                    created_at=v.get("when"),
                    message=v.get("message") or None,
                )
                for v in items
            ]
        return sorted(out, key=lambda v: v.number)

    def live_page(self, page_id: str) -> LivePage:
        """Current status, space, labels, ancestors and own + inherited read restrictions of one page."""
        if self.cloud:
            data = self._get(f"{self.v2}/pages/{int(page_id)}", **{"include-labels": "true"})
            labels = [lbl["name"] for lbl in (data.get("labels") or {}).get("results", [])]
            ancestors = [
                str(a["id"])
                for a in self._all(f"{self.v2}/pages/{int(page_id)}/ancestors", {"limit": PAGE_SIZE}, 5)
                if a.get("type", "page") == "page"
            ]
            space_id, own = str(data.get("spaceId", "")), self.restrictions(page_id)
        else:
            data = self._get(
                f"{self.v1}/content/{int(page_id)}",
                expand="space,ancestors,metadata.labels,restrictions.read.restrictions.user,"
                "restrictions.read.restrictions.group",
            )
            labels = [
                lbl["name"] for lbl in ((data.get("metadata") or {}).get("labels") or {}).get("results", [])
            ]
            ancestors = [str(a["id"]) for a in data.get("ancestors") or []]
            space_id = str((data.get("space") or {}).get("id", ""))
            own = self._acl(((data.get("restrictions") or {}).get("read") or {}).get("restrictions") or {})
        inherited = []
        for ancestor in ancestors:
            acl = self.restrictions(ancestor)
            if acl["users"] or acl["groups"]:
                inherited.append({"page_id": ancestor, **acl})
        return LivePage(
            id=str(data["id"]),
            status=data.get("status", "current"),
            space_id=space_id,
            labels=sorted(labels),
            ancestors=ancestors,
            restrictions=own,
            inherited=inherited,
        )
