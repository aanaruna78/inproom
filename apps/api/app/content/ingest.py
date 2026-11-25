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

from sqlalchemy import Connection, insert, select, update

from app.connectors.confluence import PageListing, Space
from app.content import store as blobs
from app.content.revisions import IN_SCOPE, record_revision
from app.db_schema import confluence_pages, outbox_events, source_objects, source_revisions

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


def current_items(conn: Connection, project_id, kind: str, source_id) -> dict[str, dict[str, Any]]:
    """external_id -> what is stored for it now: current sha256 (None when the current revision's bytes were
    purged: INPROOM-161), object status, ACL, title and URI."""
    rows = conn.execute(
        select(
            source_objects.c.external_id,
            source_objects.c.status,
            source_objects.c.acl,
            source_objects.c.title,
            source_objects.c.uri,
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
    return {
        r.external_id: {
            "sha": None if r.purged_at else r.content_sha256,
            "status": r.status,
            "acl": r.acl,
            "title": r.title,
            "uri": r.uri,
        }
        for r in rows
    }


def _unchanged(have: dict[str, Any] | None, data: bytes, title: str, uri: str | None, acl: dict) -> bool:
    """Nothing to write: same bytes, in scope, and the same ACL, title and URI (INPROOM-163: an ACL-only
    change must still reach the stored object)."""
    return have is not None and have == {
        "sha": blobs.sha256(data),
        "status": IN_SCOPE,
        "acl": acl,
        "title": title,
        "uri": uri,
    }


def refresh_acl(
    conn: Connection, tenant, project_id, kind: str, source_id, external_id: str, acl: dict
) -> None:
    """Keep an out-of-scope item's current ACL true (its bytes are withheld anyway); revisions keep the ACL
    they were fetched under."""
    obj = (
        conn.execute(
            update(source_objects)
            .where(
                source_objects.c.project_id == project_id,
                source_objects.c.source_kind == kind,
                source_objects.c.source_id == source_id,
                source_objects.c.external_id == external_id,
            )
            .values(acl=acl, updated_at=datetime.now(UTC))
            .returning(source_objects.c.id)
        )
        .mappings()
        .first()
    )
    if obj is not None:
        acl_changed_event(conn, tenant, project_id, kind, source_id, external_id, obj["id"], acl)


def acl_changed_event(
    conn: Connection, tenant, project_id, kind, source_id, external_id, object_id, acl
) -> None:
    conn.execute(
        insert(outbox_events).values(
            tenant_id=tenant,
            type="source.object.acl_changed",
            payload={
                "project_id": str(project_id),
                "source_kind": kind,
                "source_id": str(source_id),
                "external_id": external_id,
                "object_id": str(object_id),
                "acl": acl,
            },
        )
    )


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
    have = current_items(conn, source["project_id"], "confluence", source["id"])
    counts = {"revisions": 0, "unchanged": 0, "withheld": 0}
    for page in listing.pages:
        row = catalog.get(page.id)
        if row is None or row.status != IN_SCOPE:
            continue
        data = page.storage.encode()
        acl = {"space_id": space.id, "space_key": space.key, "restrictions": row.restrictions}
        if _unchanged(have.get(page.id), data, page.title, page.web_url, acl):
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
            acl=acl,
            job_id=job_id,
        )
        counts["revisions" if done.created else "unchanged"] += 1
    by_status: dict[str, list[str]] = {}
    for page_id, row in catalog.items():
        if row.status != IN_SCOPE and page_id in have:
            by_status.setdefault(row.status, []).append(page_id)
            acl = {"space_id": space.id, "space_key": space.key, "restrictions": row.restrictions}
            if row.status != "DELETED" and have[page_id]["acl"] != acl:  # e.g. newly restricted
                refresh_acl(conn, tenant, source["project_id"], "confluence", source["id"], page_id, acl)
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
    have = current_items(conn, source["project_id"], "jira", source["id"])
    counts = {"revisions": 0, "unchanged": 0, "withheld": 0}
    for issue in issues:
        data = jira_record(issue, fields)
        external = str(issue["id"])
        security = (issue.get("fields") or {}).get("security") or {}
        acl = {
            "jira_project_id": source["jira_project_id"],
            "jira_key": source["jira_key"],
            "security_level": security.get("name"),
        }
        title = f"{issue.get('key')}: {(issue.get('fields') or {}).get('summary') or ''}"[:500]
        uri = f"/browse/{issue.get('key')}"
        if _unchanged(have.get(external), data, title, uri, acl):
            counts["unchanged"] += 1
            continue
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
            title=title,
            uri=uri,
            provider_version=(issue.get("fields") or {}).get("updated"),
            acl=acl,
            job_id=job_id,
        )
        counts["revisions" if done.created else "unchanged"] += 1
    counts["withheld"] += mark(
        conn, source["project_id"], "jira", source["id"], deleted, "DELETED", "deleted or moved out of Jira"
    )
    _schedule_purge(conn, tenant, source["project_id"])
    return counts
