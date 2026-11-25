"""Feed the source syncs into the content store (INPROOM-158 / story INPROOM-27).

Runs inside the sync's own transaction (under its project lock), so the catalog, the revision rows and their
outbox events commit together; an object store failure rolls the whole sync back and the worker retries.

- Confluence: in-scope (CURRENT) pages store their storage-format body, which the listing already carries.
  Pages that are RESTRICTED (and not included), EXCLUDED or DELETED in the catalog only change the object's
  status; a restricted page that was never in scope is never stored.
- Jira: fetched issues store a canonical JSON record (fixed field set, sorted keys, nested issues reduced to
  id/key and people to accountId/displayName, so a linked issue's or avatar's change is not this issue's
  change). Deleted issues mark the object DELETED.

A cheap pre-check (hash of the new bytes vs the object's current revision) skips the object store entirely for
unchanged items; record_revision re-checks under the row lock, so the pre-check is only an optimisation.
"""

import json
import uuid
from collections.abc import Iterable
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import Connection, select, update

from app.connectors.confluence import PageListing, Space
from app.content import store as blobs
from app.content.revisions import IN_SCOPE, record_revision
from app.db_schema import confluence_pages, source_objects, source_revisions

CONFLUENCE_MEDIA = "application/vnd.atlassian.confluence.storage+xml; charset=utf-8"
JIRA_MEDIA = "application/vnd.inproom.jira-issue+json"
JIRA_RECORD_FIELDS = (
    "summary",
    "description",
    "environment",
    "issuetype",
    "status",
    "resolution",
    "priority",
    "labels",
    "components",
    "fixVersions",
    "versions",
    "parent",
    "issuelinks",
    "subtasks",
    "assignee",
    "reporter",
    "creator",
    "created",
    "updated",
    "duedate",
    "security",
)
# Keys that change without the issue changing (URLs, avatars, icons) or carry personal data we do not need.
_DROP = {"self", "avatarUrls", "iconUrl", "emailAddress", "timeZone", "expand", "active", "accountType"}
_MAPPED = ("story_points", "epic_link", "sprint", "acceptance_criteria", "team")


def jira_fields(mapping: dict[str, Any] | None) -> list[str]:
    """The record field set plus the custom fields the source's mapping names."""
    custom = []
    for name, entry in ((mapping or {}).get("fields") or {}).items():
        if name in _MAPPED and isinstance(entry, dict) and entry.get("field_id"):
            custom.append(entry["field_id"])
    return [*JIRA_RECORD_FIELDS, *sorted(set(custom))]


def _clean(value: Any) -> Any:
    if isinstance(value, dict):
        return {k: _clean(v) for k, v in value.items() if k not in _DROP}
    if isinstance(value, list):
        return [_clean(v) for v in value]
    return value


def _ref(issue: Any) -> Any:
    return {"id": str(issue.get("id")), "key": issue.get("key")} if isinstance(issue, dict) else issue


def jira_record(issue: dict[str, Any], fields: list[str]) -> bytes:
    """Canonical bytes for one issue: equal issues always hash equally."""
    raw = issue.get("fields") or {}
    out: dict[str, Any] = {}
    for name in fields:
        if name not in raw:
            continue
        value = raw[name]
        if name == "parent":
            value = _ref(value)
        elif name == "subtasks":
            value = [_ref(v) for v in value or []]
        elif name == "issuelinks":
            value = [
                {
                    "id": str(link.get("id")),
                    "type": (link.get("type") or {}).get("name"),
                    **{side: _ref(link[side]) for side in ("inwardIssue", "outwardIssue") if link.get(side)},
                }
                for link in value or []
            ]
        out[name] = _clean(value)
    record = {"id": str(issue["id"]), "key": issue.get("key"), "fields": out}
    return json.dumps(record, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()


def current_hashes(conn: Connection, project_id, kind: str, source_id) -> dict[str, tuple[str | None, str]]:
    """external_id -> (current revision sha256, object status) for one source. A purged current revision has
    no stored bytes, so it never matches (its item must be re-recorded: INPROOM-161)."""
    rows = conn.execute(
        select(
            source_objects.c.external_id,
            source_objects.c.status,
            source_revisions.c.content_sha256,
            source_revisions.c.purged_at,
        )
        .select_from(
            source_objects.outerjoin(
                source_revisions, source_revisions.c.id == source_objects.c.current_revision_id
            )
        )
        .where(
            source_objects.c.project_id == project_id,
            source_objects.c.source_kind == kind,
            source_objects.c.source_id == source_id,
        )
    )
    return {r.external_id: (None if r.purged_at else r.content_sha256, r.status) for r in rows}


def mark(
    conn: Connection,
    project_id,
    kind: str,
    source_id,
    external_ids: Iterable[str],
    status: str,
    reason: str | None,
) -> int:
    """Set the status of existing objects (their lineage stays; content is withheld while not CURRENT)."""
    ids = sorted(set(external_ids))
    if not ids:
        return 0
    now = datetime.now(UTC)
    return conn.execute(
        update(source_objects)
        .where(
            source_objects.c.project_id == project_id,
            source_objects.c.source_kind == kind,
            source_objects.c.source_id == source_id,
            source_objects.c.external_id.in_(ids),
            source_objects.c.status != status,
        )
        .values(
            status=status,
            status_reason=reason,
            updated_at=now,
            deleted_at=now if status == "DELETED" else None,
        )
    ).rowcount


def _schedule_purge(conn: Connection, tenant: uuid.UUID, project_id) -> None:
    from app.content import retention  # late: retention -> worker -> handlers -> here

    retention.maybe_schedule(conn, tenant, project_id)


def confluence(
    conn: Connection,
    store: blobs.BlobStore,
    tenant: uuid.UUID,
    source: dict[str, Any],
    space: Space,
    listing: PageListing,
    job_id: uuid.UUID | None = None,
) -> dict[str, int]:
    catalog = {
        r.page_id: r
        for r in conn.execute(
            select(
                confluence_pages.c.page_id,
                confluence_pages.c.status,
                confluence_pages.c.status_reason,
                confluence_pages.c.restrictions,
            ).where(confluence_pages.c.source_id == source["id"])
        )
    }
    have = current_hashes(conn, source["project_id"], "confluence", source["id"])
    counts = {"revisions": 0, "unchanged": 0, "withheld": 0}
    for page in listing.pages:
        row = catalog.get(page.id)
        if row is None or row.status != IN_SCOPE:
            continue
        data = page.storage.encode()
        if have.get(page.id) == (blobs.sha256(data), IN_SCOPE):
            counts["unchanged"] += 1
            continue
        done = record_revision(
            conn,
            store,
            tenant=tenant,
            project_id=source["project_id"],
            source_kind="confluence",
            source_id=source["id"],
            external_id=page.id,
            data=data,
            media_type=CONFLUENCE_MEDIA,
            title=page.title,
            uri=page.web_url,
            provider_version=str(page.version),
            acl={"space_id": space.id, "space_key": space.key, "restrictions": row.restrictions},
            job_id=job_id,
        )
        counts["revisions" if done.created else "unchanged"] += 1
    by_status: dict[str, list[str]] = {}
    for page_id, row in catalog.items():
        if row.status != IN_SCOPE and page_id in have:
            by_status.setdefault(row.status, []).append(page_id)
    for status, ids in by_status.items():
        reason = {"DELETED": "deleted or no longer visible in Confluence"}.get(status)
        counts["withheld"] += mark(
            conn, source["project_id"], "confluence", source["id"], ids, status, reason or status.lower()
        )
    _schedule_purge(conn, tenant, source["project_id"])
    return counts


def jira(
    conn: Connection,
    store: blobs.BlobStore,
    tenant: uuid.UUID,
    source: dict[str, Any],
    issues: list[dict[str, Any]],
    deleted: list[str],
    fields: list[str],
    job_id: uuid.UUID | None = None,
) -> dict[str, int]:
    have = current_hashes(conn, source["project_id"], "jira", source["id"])
    counts = {"revisions": 0, "unchanged": 0, "withheld": 0}
    for issue in issues:
        data = jira_record(issue, fields)
        external = str(issue["id"])
        if have.get(external) == (blobs.sha256(data), IN_SCOPE):
            counts["unchanged"] += 1
            continue
        security = (issue.get("fields") or {}).get("security") or {}
        done = record_revision(
            conn,
            store,
            tenant=tenant,
            project_id=source["project_id"],
            source_kind="jira",
            source_id=source["id"],
            external_id=external,
            data=data,
            media_type=JIRA_MEDIA,
            title=f"{issue.get('key')}: {(issue.get('fields') or {}).get('summary') or ''}"[:500],
            uri=f"/browse/{issue.get('key')}",
            provider_version=(issue.get("fields") or {}).get("updated"),
            acl={
                "jira_project_id": source["jira_project_id"],
                "jira_key": source["jira_key"],
                "security_level": security.get("name"),
            },
            job_id=job_id,
        )
        counts["revisions" if done.created else "unchanged"] += 1
    counts["withheld"] += mark(
        conn, source["project_id"], "jira", source["id"], deleted, "DELETED", "deleted or moved out of Jira"
    )
    _schedule_purge(conn, tenant, source["project_id"])
    return counts
