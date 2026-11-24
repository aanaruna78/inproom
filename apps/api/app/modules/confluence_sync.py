"""Confluence catalog sync planning (INPROOM-135 / story INPROOM-24). Pure: no I/O.

Given what the provider lists for a space (current pages only) and what the catalog already holds, decide per
page what to write. Statuses:
- CURRENT    in scope: matches every filter, and is unrestricted (or restricted pages are included);
- RESTRICTED matches the filters but carries read restrictions (own or inherited) and they are not included;
- EXCLUDED   was in the catalog but no longer matches the filters (moved, label removed, filters changed);
- DELETED    was in the catalog but is no longer listed (deleted, trashed, archived or hidden from us).
Pages that never matched the filters are not catalogued at all. A truncated listing never tombstones anything,
because absence from an incomplete listing proves nothing.
"""

from dataclasses import dataclass, field
from typing import Any

from app.connectors.confluence import PageListing, PageMeta

IN_SCOPE = "CURRENT"


@dataclass
class Filters:
    root_page_ids: list[str] = field(default_factory=list)
    required_labels: list[str] = field(default_factory=list)
    include_restricted: bool = False

    @classmethod
    def of(cls, data: dict[str, Any] | None) -> "Filters":
        data = data or {}
        return cls(
            root_page_ids=sorted(str(i) for i in data.get("root_page_ids") or []),
            required_labels=sorted(str(i).lower() for i in data.get("required_labels") or []),
            include_restricted=bool(data.get("include_restricted")),
        )

    def as_dict(self) -> dict[str, Any]:
        return {
            "root_page_ids": self.root_page_ids,
            "required_labels": self.required_labels,
            "include_restricted": self.include_restricted,
        }


@dataclass
class Plan:
    inserts: list[dict[str, Any]] = field(default_factory=list)
    updates: list[dict[str, Any]] = field(default_factory=list)
    versions: list[dict[str, Any]] = field(default_factory=list)  # new version rows
    events: list[tuple[str, dict[str, Any]]] = field(default_factory=list)  # (outbox type, payload)
    counts: dict[str, int] = field(
        default_factory=lambda: dict.fromkeys(
            ("added", "updated", "restored", "deleted", "restricted", "excluded", "unchanged"), 0
        )
    )
    truncated: bool = False

    @property
    def changed(self) -> bool:
        return bool(self.inserts or self.updates or self.versions)


def acl(page: PageMeta, by_id: dict[str, PageMeta]) -> dict[str, Any]:
    """Own read restrictions plus those inherited from ancestors (Confluence restrictions cascade down)."""
    inherited = []
    for ancestor_id in page.ancestors:
        ancestor = by_id.get(ancestor_id)
        if ancestor and (ancestor.restrictions["users"] or ancestor.restrictions["groups"]):
            inherited.append({"page_id": ancestor_id, "title": ancestor.title, **ancestor.restrictions})
    own = page.restrictions
    return {
        "own": own,
        "inherited": inherited,
        "restricted": bool(own["users"] or own["groups"] or inherited),
    }


def matches(page: PageMeta, filters: Filters) -> bool:
    in_tree = not filters.root_page_ids or any(p in filters.root_page_ids for p in [page.id, *page.ancestors])
    labels = {label.lower() for label in page.labels}
    return in_tree and all(label in labels for label in filters.required_labels)


def desired(page: PageMeta, by_id: dict[str, PageMeta], filters: Filters) -> dict[str, Any]:
    restrictions = acl(page, by_id)
    status, reason = IN_SCOPE, None
    if restrictions["restricted"] and not filters.include_restricted:
        status, reason = "RESTRICTED", "page has read restrictions (own or inherited)"
    return {
        "page_id": page.id,
        "title": page.title,
        "parent_page_id": page.parent_id,
        "ancestors": page.ancestors,
        "status": status,
        "status_reason": reason,
        "current_version": page.version,
        "content_sha256": page.content_sha256,
        "labels": page.labels,
        "restrictions": restrictions,
        "attachments": [
            {"id": a.id, "title": a.title, "media_type": a.media_type, "size": a.size, "version": a.version}
            for a in page.attachments
        ],
        "author": page.author,
        "last_modified_at": page.created_at,
        "web_url": page.web_url,
    }


COMPARED = (
    "title",
    "parent_page_id",
    "ancestors",
    "status",
    "status_reason",
    "current_version",
    "content_sha256",
    "labels",
    "restrictions",
    "attachments",
    "author",
    "last_modified_at",
    "web_url",
)


def plan_sync(existing: dict[str, dict[str, Any]], listing: PageListing, filters: Filters) -> Plan:
    plan = Plan(truncated=listing.truncated)
    by_id = {p.id: p for p in listing.pages}
    seen: set[str] = set()

    def event(kind: str, row: dict[str, Any], **extra: Any) -> None:
        plan.events.append(
            (
                f"source.confluence.page.{kind}",
                {"page_id": row["page_id"], "title": row["title"], "version": row.get("current_version")}
                | extra,
            )
        )

    def version_row(page: PageMeta) -> dict[str, Any]:
        return {
            "page_id": page.id,
            "version": page.version,
            "content_sha256": page.content_sha256,
            "title": page.title,
            "author": page.author,
            "created_at": page.created_at,
            "message": page.message,
        }

    for page in listing.pages:
        if not matches(page, filters):
            continue
        seen.add(page.id)
        want = desired(page, by_id, filters)
        have = existing.get(page.id)
        if have is None:
            plan.inserts.append(want)
            plan.versions.append(version_row(page))
            if want["status"] == IN_SCOPE:
                plan.counts["added"] += 1
                event("changed", want)
            else:
                plan.counts["restricted"] += 1
            continue
        if all(have.get(k) == want[k] for k in COMPARED):
            plan.counts["unchanged"] += 1
            continue
        plan.updates.append(want)
        new_version = (have["current_version"], have["content_sha256"]) != (page.version, page.content_sha256)
        if new_version:
            plan.versions.append(version_row(page))
        if want["status"] == IN_SCOPE and have["status"] != IN_SCOPE:
            plan.counts["restored"] += 1
            event("changed", want, reason="back in scope")
        elif want["status"] != IN_SCOPE and have["status"] == IN_SCOPE:
            plan.counts["restricted"] += 1
            event("out_of_scope", want, reason=want["status_reason"])
        elif new_version and want["status"] == IN_SCOPE:
            plan.counts["updated"] += 1
            event("changed", want)
        else:
            plan.counts["updated"] += 1  # metadata only (labels, attachments, title, restrictions)

    if listing.truncated:
        return plan  # absence from an incomplete listing proves nothing
    for page_id, have in existing.items():
        if page_id in seen:
            continue
        listed = page_id in by_id
        target = "EXCLUDED" if listed else "DELETED"
        if have["status"] == target:
            continue
        reason = (
            "no longer matches the space filters"
            if listed
            else "no longer listed (deleted, trashed, archived or hidden from this credential)"
        )
        plan.updates.append({"page_id": page_id, "status": target, "status_reason": reason})
        plan.counts["excluded" if listed else "deleted"] += 1
        if have["status"] == IN_SCOPE or target == "DELETED":
            event("deleted" if target == "DELETED" else "out_of_scope", have, reason=reason)
    return plan
