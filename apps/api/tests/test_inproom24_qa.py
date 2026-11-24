"""QA probes for INPROOM-24 (build/qa/INPROOM-24/test_probes.py at fe4e876), committed as regressions.

Unfixed cases are strict xfails naming their defect, so each fix must flip them to passing:
- INPROOM-139: live content must be revalidated against the current approved scope;
- INPROOM-140: archived projects must not allow space removal (fixed; also for repositories and Jira).
One probe is adapted (see test_unchanged_sync_writes_no_catalog_rows_or_page_events) because AC3 was
clarified rather than changed: an unchanged sync records when it ran (source sync timestamp + one audit row)
but writes no page/version rows and emits no page events.
"""

import pytest

from tests import test_confluence_sources_integration as helpers

env = helpers.env
selected = helpers.selected

pytestmark = pytest.mark.integration
INPROOM_139 = pytest.mark.xfail(strict=True, reason="INPROOM-139: live content not revalidated")


@pytest.mark.parametrize(
    "change",
    [
        pytest.param("own_restriction", marks=INPROOM_139),
        pytest.param("inherited_restriction", marks=INPROOM_139),
        pytest.param("label_removed", marks=INPROOM_139),
        pytest.param("archived", marks=INPROOM_139),
        pytest.param("draft", marks=INPROOM_139),
    ],
)
def test_live_content_revalidates_approved_scope(selected, change):
    e = selected
    if change == "own_restriction":
        e.conf.pages[101].groups = ["bots"]
    elif change == "inherited_restriction":
        e.conf.pages[100].groups = ["bots"]
    elif change == "label_removed":
        e.conf.pages[101].labels = []
    else:
        e.conf.pages[101].status = change
    r = e.client.get(f"/v1/projects/{e.pid}/sources/confluence/{e.sid}/pages/101/content", headers=e.h())
    assert r.status_code in (
        404,
        409,
    ), f"{change}: expected content refusal, got {r.status_code}; version={r.json().get('version')}"


@INPROOM_139
def test_restriction_after_last_sync_cannot_be_bypassed_with_old_version(selected):
    e = selected
    e.conf.pages[101].groups = ["bots"]
    r = e.client.get(
        f"/v1/projects/{e.pid}/sources/confluence/{e.sid}/pages/101/content?version=1", headers=e.h()
    )
    assert r.status_code in (404, 409), f"expected content refusal, got {r.status_code}"


def test_archived_project_cannot_deselect_space(selected):
    e = selected
    r = e.client.post(f"/v1/projects/{e.pid}/archive", headers=e.h())
    assert r.status_code == 200, r.text
    r = e.client.delete(f"/v1/projects/{e.pid}/sources/confluence/{e.cid}:ENG", headers=e.h())
    assert (
        r.status_code == 409
    ), f"expected PROJECT_ARCHIVED; got {r.status_code}; catalog remaining={len(e.pages())}"


def test_unchanged_sync_writes_no_catalog_rows_or_page_events(selected):
    """Adapted from QA's test_unchanged_sync_writes_nothing_including_source_and_audit: AC3 is clarified to
    'no page/version rows and no page events'; recording that a sync ran (last_synced_at, one audit row)
    is intended, so it is asserted explicitly here instead of forbidden."""
    e = selected
    pages = e.q("SELECT max(updated_at)::text FROM confluence_pages WHERE tenant_id=:t")
    versions = e.q("SELECT count(*) FROM confluence_page_versions WHERE tenant_id=:t")
    events = e.q(
        "SELECT count(*) FROM outbox_events WHERE tenant_id=:t AND type LIKE 'source.confluence.page.%'"
    )
    synced = e.q("SELECT last_synced_at::text FROM project_confluence_sources WHERE tenant_id=:t")
    audits = e.q("SELECT count(*) FROM audit_events WHERE tenant_id=:t AND action='source.confluence.synced'")
    r = e.sync()
    assert r.status_code == 200 and r.json()["changed"] is False
    assert e.q("SELECT max(updated_at)::text FROM confluence_pages WHERE tenant_id=:t") == pages
    assert e.q("SELECT count(*) FROM confluence_page_versions WHERE tenant_id=:t") == versions
    assert (
        e.q("SELECT count(*) FROM outbox_events WHERE tenant_id=:t AND type LIKE 'source.confluence.page.%'")
        == events
    )
    assert e.q("SELECT last_synced_at::text FROM project_confluence_sources WHERE tenant_id=:t") != synced
    assert (
        e.q("SELECT count(*) FROM audit_events WHERE tenant_id=:t AND action='source.confluence.synced'")
        == audits + 1
    )


@pytest.mark.parametrize(("failure", "expected"), [("ratelimit", 503), ("timeout", 502), ("500", 502)])
def test_second_provider_page_failure_is_atomic(selected, failure, expected):
    e = selected
    e.conf.edit(101, "<p>changed before failed sync</p>")
    before = {p: (r.status, r.current_version) for p, r in e.pages().items()}
    events = e.events("source.confluence.page.changed")
    # get_space succeeds, first page succeeds, second page fails on every retry.
    e.conf.fail_after = 2
    e.conf.failures = [failure] * 3
    start = len(e.conf.calls)
    r = e.sync()
    assert r.status_code == expected, r.text
    assert len(e.conf.calls) - start >= 3
    assert {p: (r.status, r.current_version) for p, r in e.pages().items()} == before
    assert e.events("source.confluence.page.changed") == events


def test_archived_project_keeps_git_and_jira_selections(db_client, tokens, fake_github, fake_jira):
    """INPROOM-140 sibling routes: repository and Jira deselection lacked the same archive guard."""
    import uuid

    from tests.auth_helpers import bearer
    from tests.fakes import jira as fj
    from tests.fakes.github import VALID_TOKEN

    h = bearer(tokens.mint(tenant_id=uuid.uuid4(), user_id="alice", roles=["tenant_admin"]))
    ws = db_client.post("/v1/workspaces", json={"slug": "arch", "name": "Arch"}, headers=h).json()
    pid = db_client.post(
        f"/v1/workspaces/{ws['id']}/projects", json={"key": "ARC", "name": "Archived"}, headers=h
    ).json()["id"]
    git = db_client.post(
        "/v1/connections", json={"display_name": "gh", "token": VALID_TOKEN}, headers=h
    ).json()["id"]
    jira = db_client.post(
        "/v1/connections",
        json={
            "provider": "jira",
            "display_name": "j",
            "base_url": "https://acme.atlassian.net",
            "email": fj.EMAIL,
            "token": fj.VALID_TOKEN,
        },
        headers=h,
    ).json()["id"]
    repo = f"/v1/projects/{pid}/sources/repositories/{git}/1"
    jira_ref = f"/v1/projects/{pid}/sources/jira/{jira}:ABC"
    assert db_client.put(repo, json={"branches": ["main"]}, headers=h).status_code == 200
    assert db_client.put(jira_ref, headers=h).status_code == 200
    assert db_client.post(f"/v1/projects/{pid}/archive", headers=h).status_code == 200
    for path in (repo, jira_ref):
        r = db_client.delete(path, headers=h)
        assert r.status_code == 409 and r.json()["code"] == "PROJECT_ARCHIVED", path
    assert len(db_client.get(f"/v1/projects/{pid}/sources/repositories", headers=h).json()) == 1
    assert len(db_client.get(f"/v1/projects/{pid}/sources/jira", headers=h).json()) == 1
