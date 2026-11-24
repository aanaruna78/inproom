"""Read-only Jira issue and dependency reader (INPROOM-132 / story INPROOM-23).

Issues of a selected, ACTIVE Jira project are read through the project's current mapping and returned in
INPROOM's normalised form: canonical type and status (raw names kept), Jira source id and key, parent, sprint
ids, estimate, acceptance criteria and dependency links with their direction. Nothing is stored here
(persisting and indexing is E03). The provider's page token is wrapped in an opaque token bound to the source
and signed, so a token cannot be replayed against another source or tampered with.
"""

import base64
import hashlib
import hmac
import json
import re
import uuid
from datetime import datetime
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Query, Request
from pydantic import BaseModel

from app.api_errors import ApiError, conflict
from app.auth import CurrentPrincipal
from app.authz import authorize
from app.connectors import vault
from app.connectors.registry import Transports, jira_for, provider_errors
from app.db import tenant_session
from app.modules.connections import credential_of, load_connection
from app.modules.jira_schema import CATEGORY
from app.modules.jira_sources import _source

router = APIRouter(tags=["jira sources"])
PROBLEM = {"description": "problem+json"}
PAGE_SIZE = 50
BASE_FIELDS = ["summary", "issuetype", "status", "parent", "labels", "issuelinks", "updated"]
_DC_SPRINT_ID = re.compile(r"\bid=(\d+)")


class IssueLink(BaseModel):
    type: str
    direction: Literal["outward", "inward"]
    relation: str  # Jira's phrase from this issue's point of view, e.g. "blocks" / "is blocked by"
    target_key: str


class JiraIssue(BaseModel):
    source_id: str
    key: str
    type: str
    type_raw: str
    status: str
    status_raw: str
    summary: str
    parent_key: str | None
    epic_key: str | None
    sprint_ids: list[str]
    story_points: float | None
    acceptance_criteria: str | None
    labels: list[str]
    links: list[IssueLink]
    updated_at: str | None


class JiraIssuePage(BaseModel):
    source_id: uuid.UUID
    jira_key: str
    mapping_version: int
    items: list[JiraIssue]
    next_page_token: str | None


# ---------------------------------------------------------------- page tokens


def _signing_key() -> bytes:
    current, keys = vault.keyring()
    return hashlib.sha256(b"inproom:jira-page-token:" + keys[current]).digest()


def encode_page_token(source_id: uuid.UUID, provider_token: str, updated_since: str | None) -> str:
    body = json.dumps({"s": str(source_id), "p": provider_token, "u": updated_since}, separators=(",", ":"))
    mac = hmac.new(_signing_key(), body.encode(), hashlib.sha256).hexdigest()[:32]
    return base64.urlsafe_b64encode(f"{body}|{mac}".encode()).decode().rstrip("=")


def decode_page_token(token: str, source_id: uuid.UUID) -> tuple[str, str | None]:
    invalid = ApiError(
        400, "INVALID_PAGE_TOKEN", "Invalid page token", "page_token is invalid for this source"
    )
    try:
        raw = base64.urlsafe_b64decode(token + "=" * (-len(token) % 4)).decode()
        body, _, mac = raw.rpartition("|")
        expected = hmac.new(_signing_key(), body.encode(), hashlib.sha256).hexdigest()[:32]
        data = json.loads(body)
    except (ValueError, UnicodeDecodeError) as exc:
        raise invalid from exc
    if not hmac.compare_digest(mac, expected) or data.get("s") != str(source_id):
        raise invalid
    return str(data["p"]), data.get("u")


# ---------------------------------------------------------------- normalisation


def adf_text(value: Any) -> str | None:
    """Plain text from a Jira Cloud rich-text (ADF) document, or the value itself if already a string."""
    if value is None or isinstance(value, str):
        return value
    if not isinstance(value, dict):
        return str(value)
    blocks: list[str] = []

    def walk(node: dict[str, Any], out: list[str]) -> None:
        if node.get("type") == "text":
            out.append(node.get("text", ""))
        elif node.get("type") == "hardBreak":
            out.append("\n")
        for child in node.get("content") or []:
            if isinstance(child, dict):
                if child.get("type") in ("paragraph", "heading", "listItem", "codeBlock"):
                    inner: list[str] = []
                    walk(child, inner)
                    blocks.append("".join(inner))
                else:
                    walk(child, out)

    top: list[str] = []
    walk(value, top)
    text = "\n".join(b for b in [*blocks, "".join(top)] if b)
    return text or None


def _sprint_ids(value: Any) -> list[str]:
    ids = []
    for item in value or []:
        if isinstance(item, dict) and "id" in item:
            ids.append(str(item["id"]))
        elif isinstance(item, str):  # Data Center: "com.atlassian.greenhopper...Sprint@1[id=12,...]"
            match = _DC_SPRINT_ID.search(item)
            if match:
                ids.append(match.group(1))
    return ids


def _field_id(mapping: dict[str, Any], name: str) -> str | None:
    entry = (mapping.get("fields") or {}).get(name)
    return entry["field_id"] if entry else None


def normalise(issue: dict[str, Any], mapping: dict[str, Any]) -> JiraIssue:
    fields = issue.get("fields") or {}
    itype = fields.get("issuetype") or {}
    status = fields.get("status") or {}
    points_id = _field_id(mapping, "story_points")
    points = fields.get(points_id) if points_id else None
    epic_id = _field_id(mapping, "epic_link")
    epic = fields.get(epic_id) if epic_id else None
    sprint_id = _field_id(mapping, "sprint")
    criteria_id = _field_id(mapping, "acceptance_criteria")
    links = []
    for link in fields.get("issuelinks") or []:
        kind = link.get("type") or {}
        if link.get("outwardIssue"):
            links.append(
                IssueLink(
                    type=kind.get("name", ""),
                    direction="outward",
                    relation=kind.get("outward", ""),
                    target_key=link["outwardIssue"]["key"],
                )
            )
        if link.get("inwardIssue"):
            links.append(
                IssueLink(
                    type=kind.get("name", ""),
                    direction="inward",
                    relation=kind.get("inward", ""),
                    target_key=link["inwardIssue"]["key"],
                )
            )
    category = (status.get("statusCategory") or {}).get("key", "indeterminate")
    return JiraIssue(
        source_id=str(issue["id"]),
        key=issue["key"],
        type=(mapping.get("issue_types") or {}).get(str(itype.get("id")), "other"),
        type_raw=itype.get("name", ""),
        status=(mapping.get("statuses") or {}).get(
            str(status.get("id")), CATEGORY.get(category, "in_progress")
        ),
        status_raw=status.get("name", ""),
        summary=fields.get("summary") or "",
        parent_key=(fields.get("parent") or {}).get("key"),
        epic_key=epic if isinstance(epic, str) else None,
        sprint_ids=_sprint_ids(fields.get(sprint_id)) if sprint_id else [],
        story_points=float(points) if isinstance(points, int | float) else None,
        acceptance_criteria=adf_text(fields.get(criteria_id)) if criteria_id else None,
        labels=[str(label) for label in fields.get("labels") or []],
        links=links,
        updated_at=fields.get("updated"),
    )


# ---------------------------------------------------------------- route


@router.get(
    "/v1/projects/{project_id}/sources/jira/{source_id}/issues",
    response_model=JiraIssuePage,
    summary="Read-only: the Jira project's issues and dependency links, normalised by the mapping (paged)",
    responses={400: PROBLEM, 404: PROBLEM, 409: PROBLEM, 502: PROBLEM, 503: PROBLEM},
)
def list_jira_issues(
    request: Request,
    principal: CurrentPrincipal,
    transports: Transports,
    project_id: uuid.UUID,
    source_id: uuid.UUID,
    page_token: Annotated[str | None, Query(max_length=2000)] = None,
    updated_since: Annotated[
        datetime | None, Query(description="Only issues updated at or after this time")
    ] = None,
) -> JiraIssuePage:
    since = updated_since.strftime("%Y-%m-%d %H:%M") if updated_since else None
    provider_token = None
    if page_token:
        provider_token, since = decode_page_token(page_token, source_id)
    with tenant_session(principal.tenant_id) as conn:
        authorize(conn, principal, request, "project.read", project_id)
        source = _source(conn, principal, project_id, source_id)
        if source["status"] != "ACTIVE":
            raise conflict(
                "SOURCE_PAUSED", f"This Jira source is paused: {source['status_reason'] or 'paused'}"
            )
        row = load_connection(conn, principal, request, source["connection_id"], "project.read")
        if row["status"] not in ("ACTIVE", "DEGRADED"):
            raise conflict("CONNECTION_NOT_ACTIVE", f"Connection is {row['status']}")
        token = credential_of(conn, principal, row)
    mapping = source["mapping"]
    wanted = BASE_FIELDS + [
        f
        for f in (
            _field_id(mapping, n) for n in ("story_points", "sprint", "epic_link", "acceptance_criteria")
        )
        if f
    ]
    jql = f'project = "{source["jira_project_id"]}"'
    if since:
        jql += f' AND updated >= "{since}"'
    jql += " ORDER BY key ASC"
    with provider_errors(
        ("JIRA_PROJECT_NOT_ACCESSIBLE", "The credential can no longer browse this Jira project")
    ):
        page = jira_for(row, token, transports).search_issues(jql, wanted, provider_token, PAGE_SIZE)
    return JiraIssuePage(
        source_id=source_id,
        jira_key=source["jira_key"],
        mapping_version=source["mapping_version"],
        items=[normalise(i, mapping) for i in page.issues],
        next_page_token=encode_page_token(source_id, page.next_page_token, since)
        if page.next_page_token
        else None,
    )
