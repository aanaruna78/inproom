"""Jira inventory normalisation and diagnostics for migration projects (INPROOM-151 / story INPROOM-19).

Pure functions. Diagnostics REPORT what cannot be resolved; nothing is invented:
- orphan            the issue names a parent that is not in the inventory (deleted, moved, or not visible)
- unsupported_type  the issue type maps to "other" (not epic / story / task / sub-task / bug)
- unresolved_link   a link points at an issue outside the inventory (another project, or not visible)
- key_changed       the key differs from the first key seen (moved or project renamed); original_key is kept
- duplicate_key     two different issue ids carry the same key in this inventory
"""

from collections import Counter, defaultdict
from typing import Any

from app.modules.jira_schema import CATEGORY, canonical_issue_type

FIELDS = [
    "summary",
    "issuetype",
    "status",
    "parent",
    "issuelinks",
    "creator",
    "reporter",
    "created",
    "updated",
]
MAX_ISSUES = 50_000
EXAMPLES = 50


def _person(value: Any) -> str | None:
    if not isinstance(value, dict):
        return None
    return value.get("displayName") or value.get("emailAddress") or value.get("accountId")


def normalise(issue: dict[str, Any], mapping: dict[str, Any] | None = None) -> dict[str, Any]:
    """One Jira search result -> an inventory row (without run bookkeeping)."""
    mapping = mapping or {}
    f = issue.get("fields") or {}
    itype = f.get("issuetype") or {}
    status = f.get("status") or {}
    type_id = str(itype.get("id", ""))
    type_name = itype.get("name") or "Unknown"
    canonical = (mapping.get("issue_types") or {}).get(type_id) or canonical_issue_type(
        type_name, int(itype.get("hierarchyLevel", 0) or 0), bool(itype.get("subtask"))
    )
    # Search results may omit hierarchyLevel; fall back on the well-known names before calling it "other".
    if canonical != "subtask" and type_name.lower() in ("sub-task", "subtask"):
        canonical = "subtask"
    elif canonical == "other" and "epic" in type_name.lower():
        canonical = "epic"
    category = (status.get("statusCategory") or {}).get("key", "indeterminate")
    parent = f.get("parent") or {}
    links = []
    for link in f.get("issuelinks") or []:
        kind = link.get("type") or {}
        for direction, side in (("outward", "outwardIssue"), ("inward", "inwardIssue")):
            target = link.get(side)
            if target:
                links.append(
                    {
                        "type": kind.get("name", ""),
                        "direction": direction,
                        "relation": kind.get(direction, ""),
                        "target_key": target.get("key"),
                        "target_id": str(target["id"]) if target.get("id") else None,
                    }
                )
    return {
        "jira_issue_id": str(issue["id"]),
        "key": issue["key"],
        "type_raw": type_name,
        "type": canonical,
        "status_raw": status.get("name") or "Unknown",
        "status": (mapping.get("statuses") or {}).get(str(status.get("id")))
        or CATEGORY.get(category, "in_progress"),
        "parent_issue_id": str(parent["id"]) if parent.get("id") else None,
        "parent_key": parent.get("key"),
        "summary": f.get("summary") or "",
        "creator": _person(f.get("creator")),
        "reporter": _person(f.get("reporter")),
        "created_at": f.get("created"),
        "updated_at": f.get("updated"),
        "links": links,
    }


def diagnose(rows: list[dict[str, Any]]) -> tuple[dict[str, list[str]], dict[str, Any], dict[str, Any]]:
    """For the PRESENT inventory rows: (flags per issue id, counts, diagnostics with examples)."""
    by_id = {r["jira_issue_id"]: r for r in rows}
    keys = Counter(r["key"] for r in rows)
    present_keys = set(keys)
    flags: dict[str, list[str]] = defaultdict(list)
    examples: dict[str, list[dict[str, Any]]] = defaultdict(list)

    def flag(row: dict[str, Any], name: str, **detail: Any) -> None:
        flags[row["jira_issue_id"]].append(name)
        if len(examples[name]) < EXAMPLES:
            examples[name].append({"key": row["key"], "id": row["jira_issue_id"]} | detail)

    for r in rows:
        if r["parent_issue_id"] or r["parent_key"]:
            known = (
                (r["parent_issue_id"] in by_id) if r["parent_issue_id"] else (r["parent_key"] in present_keys)
            )
            if not known:
                flag(r, "orphan", parent_key=r["parent_key"], parent_id=r["parent_issue_id"])
        if r["type"] == "other":
            flag(r, "unsupported_type", type=r["type_raw"])
        missing = sorted(
            {
                link["target_key"]
                for link in r["links"]
                if link.get("target_key")
                and link["target_key"] not in present_keys
                and (link.get("target_id") is None or link["target_id"] not in by_id)
            }
        )
        if missing:
            flag(r, "unresolved_link", targets=missing)
        if r.get("original_key") and r["original_key"] != r["key"]:
            flag(r, "key_changed", original_key=r["original_key"])
        if keys[r["key"]] > 1:
            flag(r, "duplicate_key")
    counts = {
        "issues": len(rows),
        "by_type": dict(sorted(Counter(r["type"] for r in rows).items())),
        "by_status": dict(sorted(Counter(r["status"] for r in rows).items())),
        "flags": dict(sorted(Counter(f for fl in flags.values() for f in fl).items())),
    }
    return dict(flags), counts, {name: items for name, items in sorted(examples.items())}
