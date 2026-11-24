"""INPROOM-132 (story INPROOM-23 AC 2, 9): the read-only Jira issue/dependency reader and the sync scope."""

import pytest

from app.modules import jira_issues
from tests import test_jira_sources_integration as helpers
from tests.fakes import jira as fj

env = helpers.env
selected = helpers.selected
pytestmark = pytest.mark.integration


def _all(env, **params):
    items, token = [], None
    while True:
        query = params | ({"page_token": token} if token else {})
        r = env.client.get(env.src("/issues"), params=query, headers=env.h())
        assert r.status_code == 200, r.text
        items += r.json()["items"]
        token = r.json()["next_page_token"]
        if not token:
            return items


def test_issues_are_normalised_with_dependencies(selected):
    issues = {i["key"]: i for i in _all(selected)}
    assert list(issues) == ["ABC-1", "ABC-2", "ABC-3", "ABC-4", "ABC-5"]
    story = issues["ABC-2"]
    assert (story["source_id"], story["type"], story["type_raw"], story["status"], story["status_raw"]) == (
        "20002",
        "story",
        "Story",
        "in_progress",
        "In Progress",
    )
    assert (story["parent_key"], story["sprint_ids"], story["story_points"]) == ("ABC-1", ["12"], 5.0)
    assert story["acceptance_criteria"].startswith("Given a customer")
    assert story["links"] == [
        {"type": "Blocks", "direction": "outward", "relation": "blocks", "target_key": "ABC-3"}
    ]
    assert issues["ABC-3"]["links"] == [
        {"type": "Blocks", "direction": "inward", "relation": "is blocked by", "target_key": "ABC-2"},
        {"type": "Relates", "direction": "outward", "relation": "relates to", "target_key": "ABC-5"},
    ]
    assert (issues["ABC-1"]["type"], issues["ABC-4"]["type"]) == ("epic", "subtask")
    assert (issues["ABC-5"]["type"], issues["ABC-5"]["status"]) == ("bug", "done")  # "Verified" is done


def test_paging_tokens_are_signed_and_bound_to_the_source(selected, monkeypatch):
    env = selected
    monkeypatch.setattr(jira_issues, "PAGE_SIZE", 2)
    assert [i["key"] for i in _all(env)] == ["ABC-1", "ABC-2", "ABC-3", "ABC-4", "ABC-5"]
    token = env.client.get(env.src("/issues"), headers=env.h()).json()["next_page_token"]
    tampered = token[:-3] + ("AAA" if not token.endswith("AAA") else "BBB")
    noep = env.select("NOEP").json()["id"]
    for bad, path in (
        (tampered, env.src("/issues")),
        ("garbage", env.src("/issues")),
        (token, f"/v1/projects/{env.pid}/sources/jira/{noep}/issues"),  # replayed against another source
    ):
        r = env.client.get(path, params={"page_token": bad}, headers=env.h())
        assert r.status_code == 400 and r.json()["code"] == "INVALID_PAGE_TOKEN"


def test_mapping_and_updated_since_drive_the_read(selected):
    env = selected
    env.client.put(
        env.src("/mapping"),
        json={"fields": {"acceptance_criteria": None, "story_points": fj.TEAM}},
        headers=env.h(**{"If-Match": '"1"'}),
    )
    issues = _all(env)
    assert all(i["acceptance_criteria"] is None and i["story_points"] is None for i in issues)
    recent = _all(env, updated_since="2026-11-21T00:00:00Z")
    assert [i["key"] for i in recent] == ["ABC-3"]


def test_scope_lists_active_jira_projects_and_excludes_paused(selected):
    env = selected
    scope = env.client.get(f"/v1/projects/{env.pid}/sources/scope", headers=env.h()).json()
    assert [(j["jira_key"], j["snapshot_version"], j["mapping_version"]) for j in scope["jira_projects"]] == [
        ("ABC", 1, 1)
    ]
    env.jira.revoke(fj.VALID_TOKEN, "ABC")
    env.client.post(f"/v1/connections/{env.cid}/check", headers=env.h())
    scope = env.client.get(f"/v1/projects/{env.pid}/sources/scope", headers=env.h()).json()
    assert scope["jira_projects"] == []
    assert [(e["kind"], e["full_name"]) for e in scope["excluded"]] == [("jira", "ABC")]
    paused = env.client.get(env.src("/issues"), headers=env.h())
    assert paused.status_code == 409 and paused.json()["code"] == "SOURCE_PAUSED"


# ---------------------------------------------------------------- pure normalisation edge cases


def test_rich_text_and_data_center_sprint_values():
    adf = {
        "type": "doc",
        "content": [
            {"type": "paragraph", "content": [{"type": "text", "text": "Given A"}]},
            {"type": "paragraph", "content": [{"type": "text", "text": "then B"}]},
        ],
    }
    assert jira_issues.adf_text(adf) == "Given A\nthen B"
    assert jira_issues.adf_text("plain") == "plain" and jira_issues.adf_text(None) is None
    dc = ["com.atlassian.greenhopper.service.sprint.Sprint@1a[id=12,rapidViewId=1,state=ACTIVE,name=S12]"]
    assert jira_issues._sprint_ids(dc) == ["12"]
