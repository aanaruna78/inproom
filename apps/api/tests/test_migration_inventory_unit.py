"""INPROOM-151: migration inventory normalisation and diagnostics (pure)."""

from app.migration.inventory import diagnose, normalise


def issue(iid, key, type_name="Story", parent=None, links=()):
    fields = {
        "summary": key,
        "issuetype": {"id": "1", "name": type_name},
        "status": {"id": "3", "name": "In Progress", "statusCategory": {"key": "indeterminate"}},
        "issuelinks": list(links),
    }
    if parent:
        fields["parent"] = parent
    return {"id": iid, "key": key, "fields": fields}


def test_normalise_keeps_jira_identity():
    row = normalise(issue("7", "LEG-7", "Sub-task", parent={"id": "1", "key": "LEG-1"}))
    assert (row["jira_issue_id"], row["key"], row["type"], row["status"], row["parent_issue_id"]) == (
        "7",
        "LEG-7",
        "subtask",
        "in_progress",
        "1",
    )


def test_every_diagnostic_is_reported_not_repaired():
    rows = [
        normalise(issue("1", "LEG-1", "Epic")),
        normalise(issue("2", "LEG-2", parent={"id": "404", "key": "GONE-1"})),
        normalise(issue("3", "LEG-3", "Spike")),
        normalise(issue("4", "LEG-4", links=[{"type": {"name": "Blocks"}, "outwardIssue": {"key": "X-1"}}])),
        normalise(issue("5", "DUP-1")),
        normalise(issue("6", "DUP-1")),
        normalise(issue("8", "NEW-8")) | {"original_key": "LEG-8"},
    ]
    for r in rows:
        r.setdefault("original_key", r["key"])
    flags, counts, diagnostics = diagnose(rows)
    assert flags["2"] == ["orphan"] and diagnostics["orphan"][0]["parent_key"] == "GONE-1"
    assert flags["3"] == ["unsupported_type"] and flags["4"] == ["unresolved_link"]
    assert flags["5"] == flags["6"] == ["duplicate_key"] and flags["8"] == ["key_changed"]
    assert "1" not in flags and counts["issues"] == 7
