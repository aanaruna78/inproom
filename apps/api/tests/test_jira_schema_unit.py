"""INPROOM-131: Jira schema normalisation, fingerprint, diff and mapping (pure functions + the fake Jira)."""

import copy

import pytest

from app.config import get_settings
from app.connectors.jira import JiraProvider
from app.connectors.vault import Secret
from app.modules import jira_schema as js
from tests.fakes import jira as fj


@pytest.fixture
def abc(monkeypatch):
    monkeypatch.setenv("INPROOM_JIRA_ALLOWED_HOSTS", "*.atlassian.net")
    get_settings.cache_clear()
    fake = fj.standard()
    p = JiraProvider(
        "https://acme.atlassian.net", Secret(fj.VALID_TOKEN), email=fj.EMAIL, transport=fake.transport()
    )
    return js.discover(p, "ABC")


@pytest.mark.parametrize(
    ("name", "level", "subtask", "expected"),
    [
        ("Epic", 1, False, "epic"),
        ("Initiative", 2, False, "epic"),
        ("Sub-task", -1, True, "subtask"),
        ("User Story", 0, False, "story"),
        ("Defect", 0, False, "bug"),
        ("Bug", 0, False, "bug"),
        ("Task", 0, False, "task"),
        ("Spike", 0, False, "other"),
    ],
)
def test_issue_type_classification(name, level, subtask, expected):
    assert js.canonical_issue_type(name, level, subtask) == expected


def test_discovery_normalises_with_source_ids(abc):
    schema, caps = abc
    assert schema["project"] == {"id": "10100", "key": "ABC", "name": "Acme Billing"}
    assert {t["name"]: t["canonical"] for t in schema["issue_types"]} == {
        "Epic": "epic",
        "Story": "story",
        "Task": "task",
        "Sub-task": "subtask",
        "Bug": "bug",
    }
    statuses = {s["name"]: s for s in schema["statuses"]}
    assert statuses["In Review"]["canonical"] == "in_progress" and statuses["Verified"]["canonical"] == "done"
    assert statuses["Verified"]["issue_type_ids"] == ["10004"]  # only the Bug workflow has it
    assert [s["id"] for s in schema["sprints"]] == ["11", "12"] and "state" not in schema["sprints"][0]
    assert caps == {"read": True, "write_capable": True, "permissions": caps["permissions"]}


def test_auto_mapping_detects_fields_by_schema(abc):
    schema, _ = abc
    mapping, warnings = js.build_mapping(schema)
    assert mapping["fields"] == {
        "story_points": {"field_id": fj.STORY_POINTS, "source": "auto"},
        "sprint": {"field_id": fj.SPRINT, "source": "auto"},
        "epic_link": {"field_id": fj.EPIC_LINK, "source": "auto"},
        "acceptance_criteria": {"field_id": fj.ACCEPTANCE, "source": "auto"},
    }
    assert warnings == []
    renamed = copy.deepcopy(schema)  # renamed story points field: still found through its custom type
    next(f for f in renamed["fields"] if f["id"] == fj.STORY_POINTS)["name"] = "Effort"
    assert js.build_mapping(renamed)[0]["fields"]["story_points"]["field_id"] == fj.STORY_POINTS


def test_overrides_apply_and_removed_targets_warn(abc):
    schema, _ = abc
    overrides = {
        "fields": {"acceptance_criteria": fj.TEAM},
        "issue_types": {"10002": "story"},
        "statuses": {},
    }
    mapping, _ = js.build_mapping(schema, overrides)
    assert mapping["fields"]["acceptance_criteria"] == {"field_id": fj.TEAM, "source": "override"}
    assert mapping["issue_types"]["10002"] == "story"
    shrunk = copy.deepcopy(schema)
    shrunk["fields"] = [f for f in shrunk["fields"] if f["id"] != fj.TEAM]
    mapping, warnings = js.build_mapping(shrunk, overrides)
    assert mapping["fields"]["acceptance_criteria"]["source"] == "auto"  # falls back to detection
    assert [w["code"] for w in warnings] == ["OVERRIDE_TARGET_REMOVED"]
    assert js.override_errors(shrunk, overrides) == [f"fields.acceptance_criteria: unknown field {fj.TEAM}"]


def test_fingerprint_is_stable_and_diff_names_changes(abc):
    schema, _ = abc
    assert js.fingerprint(schema) == js.fingerprint(copy.deepcopy(schema))
    assert js.diff(schema, schema) == {} and js.diff(None, schema) == {}
    new = copy.deepcopy(schema)
    new["fields"] = [f for f in new["fields"] if f["id"] != fj.TEAM]
    new["statuses"].append(
        {
            "id": "999",
            "name": "Blocked",
            "category": "indeterminate",
            "canonical": "in_progress",
            "issue_type_ids": ["10001"],
        }
    )
    new["boards"][0]["name"] = "Renamed board"
    assert js.fingerprint(new) != js.fingerprint(schema)
    assert js.diff(schema, new) == {
        "fields": {"removed": [{"id": fj.TEAM, "name": "Team"}]},
        "statuses": {"added": [{"id": "999", "name": "Blocked"}]},
        "boards": {"changed": [{"id": "1", "name": "Renamed board"}]},
    }
