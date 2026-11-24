"""Jira schema discovery, normalisation, fingerprint, diff and mapping (INPROOM-131 / story INPROOM-23).

Pure functions apart from `discover`, which reads through the adapter. The normalised schema keeps Jira's own
ids (source ids) next to INPROOM's canonical vocabulary:
- issue types -> epic | story | task | subtask | bug | other (by hierarchy level first, then by name);
- statuses -> todo | in_progress | done (from Jira's status category);
- well-known fields are detected by their schema (custom type), never by display name alone.
Sprints keep only id, name and board: their state changes every few weeks and is sprint data (E08).
"""

import hashlib
import json
from typing import Any

from app.connectors.jira import READ_PERMISSION, WRITE_PERMISSIONS, JiraProvider

ISSUE_TYPES = ("epic", "story", "task", "subtask", "bug", "other")
STATUSES = ("todo", "in_progress", "done")
CANONICAL_FIELDS = ("story_points", "sprint", "epic_link", "acceptance_criteria")
SECTIONS = ("issue_types", "statuses", "fields", "link_types", "boards", "sprints")

STORY_POINT_TYPES = {"com.pyxis.greenhopper.jira:jsw-story-points"}
STORY_POINT_NAMES = {"story points", "story point estimate"}
SPRINT_TYPE = "com.pyxis.greenhopper.jira:gh-sprint"
EPIC_LINK_TYPE = "com.pyxis.greenhopper.jira:gh-epic-link"
CATEGORY = {"new": "todo", "indeterminate": "in_progress", "done": "done"}


def canonical_issue_type(name: str, hierarchy_level: int, subtask: bool) -> str:
    if subtask or hierarchy_level < 0:
        return "subtask"
    if hierarchy_level >= 1:
        return "epic"
    lowered = name.lower()
    for word, kind in (("bug", "bug"), ("defect", "bug"), ("story", "story"), ("task", "task")):
        if word in lowered:
            return kind
    return "other"


def discover(provider: JiraProvider, key: str) -> tuple[dict[str, Any], dict[str, Any]]:
    """(normalised schema, capabilities) for one Jira project. Raises adapter errors untouched."""
    project, types = provider.get_project(key)
    permissions = provider.my_permissions(project.key)
    statuses: dict[str, dict[str, Any]] = {}
    for type_id, items in provider.statuses(project.key).items():
        for st in items:
            entry = statuses.setdefault(
                st.id,
                {
                    "id": st.id,
                    "name": st.name,
                    "category": st.category,
                    "canonical": CATEGORY.get(st.category, "in_progress"),
                    "issue_type_ids": [],
                },
            )
            entry["issue_type_ids"].append(type_id)
    boards = provider.boards(project.key)
    sprints = [s for b in boards for s in provider.sprints(b)]
    schema = {
        "project": {"id": project.id, "key": project.key, "name": project.name},
        "issue_types": sorted(
            (
                {
                    "id": t.id,
                    "name": t.name,
                    "hierarchy_level": t.hierarchy_level,
                    "subtask": t.subtask,
                    "canonical": canonical_issue_type(t.name, t.hierarchy_level, t.subtask),
                }
                for t in types
            ),
            key=lambda t: t["id"],
        ),
        "statuses": sorted(
            ({**s, "issue_type_ids": sorted(s["issue_type_ids"])} for s in statuses.values()),
            key=lambda s: s["id"],
        ),
        "fields": sorted(
            (
                {
                    "id": f.id,
                    "name": f.name,
                    "custom": f.custom,
                    "schema_type": f.schema_type,
                    "custom_type": f.custom_type,
                }
                for f in provider.fields()
            ),
            key=lambda f: f["id"],
        ),
        "link_types": sorted(
            (
                {"id": lt.id, "name": lt.name, "inward": lt.inward, "outward": lt.outward}
                for lt in provider.link_types()
            ),
            key=lambda lt: lt["id"],
        ),
        "boards": sorted(
            ({"id": b.id, "name": b.name, "type": b.type} for b in boards), key=lambda b: b["id"]
        ),
        "sprints": sorted(
            ({"id": s.id, "name": s.name, "board_id": s.board_id} for s in sprints), key=lambda s: s["id"]
        ),
    }
    write = {p: permissions.get(p, False) for p in WRITE_PERMISSIONS}
    capabilities = {
        "read": permissions.get(READ_PERMISSION, False),
        "write_capable": any(write.values()),
        "permissions": {READ_PERMISSION: permissions.get(READ_PERMISSION, False)} | write,
    }
    return schema, capabilities


def fingerprint(schema: dict[str, Any]) -> str:
    return hashlib.sha256(json.dumps(schema, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def diff(old: dict[str, Any] | None, new: dict[str, Any]) -> dict[str, Any]:
    """Per section: ids added, removed and changed (same id, new content). Empty sections are omitted."""
    if old is None:
        return {}
    out: dict[str, Any] = {}
    for section in SECTIONS:
        before = {i["id"]: i for i in old.get(section, [])}
        after = {i["id"]: i for i in new.get(section, [])}
        change = {
            "added": [{"id": i, "name": after[i]["name"]} for i in sorted(after.keys() - before.keys())],
            "removed": [{"id": i, "name": before[i]["name"]} for i in sorted(before.keys() - after.keys())],
            "changed": [
                {"id": i, "name": after[i]["name"]}
                for i in sorted(after.keys() & before.keys())
                if after[i] != before[i]
            ],
        }
        change = {k: v for k, v in change.items() if v}
        if change:
            out[section] = change
    return out


def _detect_fields(fields: list[dict[str, Any]]) -> dict[str, dict[str, str] | None]:
    def first(predicate) -> dict[str, str] | None:
        found = next((f for f in fields if predicate(f)), None)
        return {"field_id": found["id"], "source": "auto"} if found else None

    return {
        "story_points": first(lambda f: f["custom_type"] in STORY_POINT_TYPES)
        or first(
            lambda f: f["custom"] and f["schema_type"] == "number" and f["name"].lower() in STORY_POINT_NAMES
        ),
        "sprint": first(lambda f: f["custom_type"] == SPRINT_TYPE),
        "epic_link": first(lambda f: f["custom_type"] == EPIC_LINK_TYPE),
        "acceptance_criteria": first(
            lambda f: f["custom"]
            and f["schema_type"] == "string"
            and f["name"].lower() == "acceptance criteria"
        ),
    }


def build_mapping(
    schema: dict[str, Any], overrides: dict[str, Any] | None = None
) -> tuple[dict[str, Any], list[dict[str, str]]]:
    """Effective mapping = automatic detection + the project admin's overrides, and the warnings that apply.
    Overrides whose target no longer exists are kept (they may come back) but not applied, and reported."""
    overrides = overrides or {"fields": {}, "issue_types": {}, "statuses": {}}
    warnings: list[dict[str, str]] = []
    field_ids = {f["id"] for f in schema["fields"]}
    fields = _detect_fields(schema["fields"])
    for name, target in (overrides.get("fields") or {}).items():
        if target is None:
            fields[name] = None
        elif target in field_ids:
            fields[name] = {"field_id": target, "source": "override"}
        else:
            warnings.append(
                {
                    "code": "OVERRIDE_TARGET_REMOVED",
                    "message": f"{name} is mapped to {target}, which no longer exists",
                }
            )
    issue_types = {t["id"]: t["canonical"] for t in schema["issue_types"]}
    statuses = {s["id"]: s["canonical"] for s in schema["statuses"]}
    for section, current in (("issue_types", issue_types), ("statuses", statuses)):
        for source_id, canonical in (overrides.get(section) or {}).items():
            if source_id in current:
                current[source_id] = canonical
            else:
                warnings.append(
                    {
                        "code": "OVERRIDE_TARGET_REMOVED",
                        "message": f"{section} override for {source_id} no longer matches the Jira project",
                    }
                )
    if "epic" not in issue_types.values():
        warnings.append({"code": "NO_EPIC_TYPE", "message": "The project has no epic-level issue type"})
    if fields["story_points"] is None:
        warnings.append({"code": "NO_STORY_POINTS_FIELD", "message": "No story points field was found"})
    if not schema["boards"]:
        warnings.append({"code": "NO_BOARD", "message": "No board: sprints are not available"})
    mapping = {
        "fields": fields,
        "issue_types": issue_types,
        "statuses": statuses,
        "overrides": overrides,
    }
    return mapping, warnings


def override_errors(schema: dict[str, Any], overrides: dict[str, Any]) -> list[str]:
    """Overrides must point at things in the current snapshot (422 MAPPING_TARGET_UNKNOWN otherwise)."""
    field_ids = {f["id"] for f in schema["fields"]}
    errors = [
        f"fields.{name}: unknown field {target}"
        for name, target in (overrides.get("fields") or {}).items()
        if target is not None and target not in field_ids
    ]
    for section in ("issue_types", "statuses"):
        known = {i["id"] for i in schema[section]}
        errors += [f"{section}.{i}: unknown id" for i in (overrides.get(section) or {}) if i not in known]
    return errors
