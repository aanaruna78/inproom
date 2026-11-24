"""Confluence page catalog and content read APIs (INPROOM-136 / story INPROOM-24).

Read-only. The catalog and page details come from INPROOM's own tables, always filtered by the selected
space's source id, so a page id from another space, project or tenant is simply not found. Content is a
read-through to Confluence for in-scope (CURRENT) pages only, and the provider's answer is checked to belong
to the selected space before anything is returned. Bodies are converted from Confluence storage format
(XHTML) to readable text; nothing is stored (that is E03).
"""

import base64
import uuid
from datetime import datetime
from html.parser import HTMLParser
from typing import Annotated, Any

from fastapi import APIRouter, Path, Query, Request
from pydantic import BaseModel
from sqlalchemy import BigInteger, cast, select

from app.api_errors import ApiError, conflict
from app.auth import CurrentPrincipal
from app.authz import authorize
from app.connectors.registry import Transports, confluence_for, provider_errors
from app.db import tenant_session
from app.db_schema import confluence_page_versions, confluence_pages
from app.modules.confluence_sources import source_of
from app.modules.connections import credential_of, load_connection

router = APIRouter(tags=["confluence sources"])
PROBLEM = {"description": "problem+json"}
PAGE_LIMIT = 100
PAGE_ID = Annotated[str, Path(pattern=r"^\d{1,20}$")]
STATUSES = ("CURRENT", "RESTRICTED", "EXCLUDED", "DELETED")


class PageSummary(BaseModel):
    page_id: str
    title: str
    status: str
    status_reason: str | None
    current_version: int
    parent_page_id: str | None
    ancestors: list[str]
    labels: list[str]
    restricted: bool
    author: str | None
    last_modified_at: str | None


class PageCatalog(BaseModel):
    source_id: uuid.UUID
    space_key: str
    items: list[PageSummary]
    next_cursor: str | None


class PageVersionInfo(BaseModel):
    version: int
    content_sha256: str
    title: str
    author: str | None
    created_at: str | None
    message: str | None
    seen_at: datetime


class PageDetail(PageSummary):
    content_sha256: str
    restrictions: dict[str, Any]
    attachments: list[dict[str, Any]]
    web_url: str | None
    versions: list[PageVersionInfo]


class PageContent(BaseModel):
    page_id: str
    title: str
    version: int
    current_version: int
    content_sha256: str
    text: str
    attachments: list[dict[str, Any]]


# ---------------------------------------------------------------- storage format -> text


class _StorageText(HTMLParser):
    """Confluence storage format to readable text: headings, paragraphs, lists, tables (one row per line),
    code kept in fences; macros and their parameters are dropped except plain-text (code) bodies."""

    BLOCKS = {"p", "div", "h1", "h2", "h3", "h4", "h5", "h6", "li", "tr", "pre", "blockquote"}

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.lines: list[str] = []
        self.current: list[str] = []
        self.cells: list[str] | None = None
        self.skip = 0  # inside a dropped macro part
        self.code = 0

    def _flush(self, prefix: str = "") -> None:
        text = " ".join("".join(self.current).split()) if not self.code else "".join(self.current).strip("\n")
        if text:
            self.lines.append(prefix + text)
        self.current = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in ("ac:parameter", "ri:attachment", "ri:page", "ri:user"):
            self.skip += 1
        elif tag in ("pre", "ac:plain-text-body"):
            self._flush()
            self.lines.append("```")
            self.code += 1
        elif tag == "tr":
            self._flush()
            self.cells = []
        elif tag in ("td", "th"):
            self.current = []
        elif tag == "br":
            self.current.append("\n" if self.code else " ")
        elif tag in self.BLOCKS:
            self._flush()

    def handle_endtag(self, tag: str) -> None:
        if tag in ("ac:parameter", "ri:attachment", "ri:page", "ri:user"):
            self.skip = max(0, self.skip - 1)
        elif tag in ("pre", "ac:plain-text-body"):
            self._flush()
            self.code = max(0, self.code - 1)
            self.lines.append("```")
        elif tag in ("td", "th") and self.cells is not None:
            self.cells.append(" ".join("".join(self.current).split()))
            self.current = []
        elif tag == "tr" and self.cells is not None:
            self.lines.append(" | ".join(self.cells))
            self.cells = None
        elif tag in ("h1", "h2", "h3", "h4", "h5", "h6"):
            self._flush("#" * int(tag[1]) + " ")
        elif tag == "li":
            self._flush("- ")
        elif tag in self.BLOCKS:
            self._flush()

    def handle_data(self, data: str) -> None:
        if not self.skip:
            self.current.append(data)

    def unknown_decl(self, data: str) -> None:  # <![CDATA[...]]> inside code macros
        if data.startswith("CDATA[") and not self.skip:
            self.current.append(data[6:])


def storage_to_text(storage: str) -> str:
    parser = _StorageText()
    parser.feed(storage)
    parser.close()
    parser._flush()
    return "\n".join(parser.lines).strip()


# ---------------------------------------------------------------- helpers


def _encode(page_id: str) -> str:
    return base64.urlsafe_b64encode(page_id.encode()).decode().rstrip("=")


def _decode(cursor: str) -> int:
    try:
        return int(base64.urlsafe_b64decode(cursor + "=" * (-len(cursor) % 4)).decode())
    except ValueError as exc:
        raise ApiError(400, "INVALID_CURSOR", "Invalid cursor", "cursor is malformed") from exc


def _summary(row: dict[str, Any]) -> dict[str, Any]:
    return dict(row) | {"restricted": bool((row.get("restrictions") or {}).get("restricted"))}


def _page(conn, tenant_id: uuid.UUID, source_id: uuid.UUID, page_id: str) -> dict[str, Any]:
    row = (
        conn.execute(
            select(confluence_pages).where(
                confluence_pages.c.tenant_id == tenant_id,
                confluence_pages.c.source_id == source_id,
                confluence_pages.c.page_id == page_id,
            )
        )
        .mappings()
        .first()
    )
    if row is None:
        raise ApiError(404, "PAGE_NOT_FOUND", "Not found", "This page is not in the selected space's catalog")
    return dict(row)


# ---------------------------------------------------------------- routes


@router.get(
    "/v1/projects/{project_id}/sources/confluence/{source_id}/pages",
    response_model=PageCatalog,
    summary="The space's page catalog (status, version, labels, restricted flag), keyset-paged",
    responses={400: PROBLEM, 404: PROBLEM},
)
def list_pages(
    request: Request,
    principal: CurrentPrincipal,
    project_id: uuid.UUID,
    source_id: uuid.UUID,
    status: Annotated[str | None, Query(pattern="^(CURRENT|RESTRICTED|EXCLUDED|DELETED)$")] = None,
    cursor: Annotated[str | None, Query(max_length=100)] = None,
) -> PageCatalog:
    with tenant_session(principal.tenant_id) as conn:
        authorize(conn, principal, request, "project.read", project_id)
        source = source_of(conn, principal, project_id, source_id)
        order = cast(confluence_pages.c.page_id, BigInteger)
        stmt = (
            select(confluence_pages)
            .where(
                confluence_pages.c.tenant_id == principal.tenant_id, confluence_pages.c.source_id == source_id
            )
            .order_by(order)
            .limit(PAGE_LIMIT + 1)
        )
        if status:
            stmt = stmt.where(confluence_pages.c.status == status)
        if cursor:
            stmt = stmt.where(order > _decode(cursor))
        rows = [dict(r) for r in conn.execute(stmt).mappings()]
    more = len(rows) > PAGE_LIMIT
    rows = rows[:PAGE_LIMIT]
    return PageCatalog(
        source_id=source_id,
        space_key=source["space_key"],
        items=[PageSummary.model_validate(_summary(r)) for r in rows],
        next_cursor=_encode(rows[-1]["page_id"]) if more else None,
    )


@router.get(
    "/v1/projects/{project_id}/sources/confluence/{source_id}/pages/{page_id}",
    response_model=PageDetail,
    summary="One catalogued page: versions seen, ACL metadata (own + inherited), attachments",
    responses={404: PROBLEM},
)
def get_page(
    request: Request,
    principal: CurrentPrincipal,
    project_id: uuid.UUID,
    source_id: uuid.UUID,
    page_id: PAGE_ID,
) -> PageDetail:
    with tenant_session(principal.tenant_id) as conn:
        authorize(conn, principal, request, "project.read", project_id)
        source = source_of(conn, principal, project_id, source_id)
        row = _page(conn, principal.tenant_id, source_id, page_id)
        versions = [
            dict(v)
            for v in conn.execute(
                select(confluence_page_versions)
                .where(
                    confluence_page_versions.c.tenant_id == principal.tenant_id,
                    confluence_page_versions.c.source_id == source_id,
                    confluence_page_versions.c.page_id == page_id,
                )
                .order_by(confluence_page_versions.c.version)
            ).mappings()
        ]
    web = row["web_url"]
    if web and web.startswith("/"):
        prefix = "/wiki" if source["base_url"].endswith(".atlassian.net") else ""
        web = f"{source['base_url']}{prefix}{web}"
    return PageDetail.model_validate(_summary(row) | {"web_url": web, "versions": versions})


@router.get(
    "/v1/projects/{project_id}/sources/confluence/{source_id}/pages/{page_id}/content",
    response_model=PageContent,
    summary="Read-through: an in-scope page's text for the current (or an earlier) version",
    responses={404: PROBLEM, 409: PROBLEM, 422: PROBLEM, 502: PROBLEM, 503: PROBLEM},
)
def get_page_content(
    request: Request,
    principal: CurrentPrincipal,
    transports: Transports,
    project_id: uuid.UUID,
    source_id: uuid.UUID,
    page_id: PAGE_ID,
    version: Annotated[int | None, Query(ge=1)] = None,
) -> PageContent:
    with tenant_session(principal.tenant_id) as conn:
        authorize(conn, principal, request, "project.read", project_id)
        source = source_of(conn, principal, project_id, source_id)
        row = _page(conn, principal.tenant_id, source_id, page_id)
        if source["status"] != "ACTIVE":
            raise conflict("SOURCE_PAUSED", f"This space is paused: {source['status_reason'] or 'paused'}")
        if row["status"] == "RESTRICTED":
            raise conflict(
                "PAGE_RESTRICTED",
                "This page has read restrictions and the space is not set to include restricted pages",
            )
        if row["status"] != "CURRENT":
            raise ApiError(
                404,
                "PAGE_NOT_IN_SCOPE",
                "Not found",
                f"This page is {row['status'].lower()}: {row['status_reason']}",
            )
        if version is not None and version > row["current_version"]:
            raise ApiError(
                404, "VERSION_NOT_FOUND", "Not found", f"The latest version is {row['current_version']}"
            )
        connection = load_connection(conn, principal, request, source["connection_id"], "project.read")
        if connection["status"] not in ("ACTIVE", "DEGRADED"):
            raise conflict("CONNECTION_NOT_ACTIVE", f"Connection is {connection['status']}")
        token = credential_of(conn, principal, connection)
    with provider_errors(("PAGE_NOT_ACCESSIBLE", "Confluence no longer returns this page to the credential")):
        body = confluence_for(connection, token, transports).get_page(page_id, version)
    if body.space_key != source["space_key"]:
        # The page moved to another space after the last sync: never serve content from outside the selection.
        raise ApiError(
            404, "PAGE_NOT_IN_SPACE", "Not found", "This page is no longer in the selected space; sync again"
        )
    return PageContent(
        page_id=page_id,
        title=body.title,
        version=body.version,
        current_version=row["current_version"],
        content_sha256=body.content_sha256,
        text=storage_to_text(body.storage),
        attachments=row["attachments"],
    )
