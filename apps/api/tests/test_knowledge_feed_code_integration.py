"""INPROOM-186 (story INPROOM-31 AC 2, 3): git branch selection in the knowledge feed, and cursor paging."""

import pytest

from tests import test_code_search_integration as code
from tests.test_knowledge_feed_integration import feed, head

pytestmark = pytest.mark.integration
env, connected, repo = code.env, code.connected, code.repo


def test_cursor_paging_and_branch_selection(repo):
    g = repo
    rid = g.q("SELECT id FROM project_repositories WHERE tenant_id=:t AND provider_repo_id='1'")
    start = head(g)
    assert g.select("1", ["main", "release/1.0"]).status_code == 200
    assert g.select("1", ["release/1.0"]).status_code == 200
    source = [x for x in feed(g, start)["items"] if x["scope"] == "source"]
    assert [(x["change"], x["branch"], x["reason"], x["source_id"]) for x in source] == [
        ("restored", "release/1.0", "BRANCH_SELECTED", str(rid)),
        ("invalidated", "main", "BRANCH_DESELECTED", str(rid)),
    ]
    total, after, seen = head(g), 0, []
    while True:  # a consumer resuming from its cursor sees every entry once, in order
        page = feed(g, after, limit=2)
        seen += [x["seq"] for x in page["items"]]
        if page["next"] is None:
            break
        after = page["next"]
    assert seen == list(range(1, total + 1))
    bad = g.client.get(f"/v1/projects/{g.pid}/knowledge/changes", params={"limit": 501}, headers=g.h())
    assert bad.status_code == 422
