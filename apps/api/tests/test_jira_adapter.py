"""INPROOM-130: Jira adapter against the fake Jira - parsing, paging, error mapping, Cloud and DC."""

import pytest

from app.config import get_settings
from app.connectors.base import (
    CredentialInvalid,
    NotFoundOrNoAccess,
    ProviderError,
    ProviderUnavailable,
    RateLimited,
)
from app.connectors.egress import EgressDenied
from app.connectors.jira import JiraProvider
from app.connectors.vault import Secret
from tests.fakes import jira as fj

SITE = "https://acme.atlassian.net"


@pytest.fixture
def fake(monkeypatch):
    monkeypatch.setenv("INPROOM_JIRA_ALLOWED_HOSTS", "*.atlassian.net, jira.corp.example.com")
    get_settings.cache_clear()
    return fj.standard()


def provider(fake, token=fj.VALID_TOKEN, email=fj.EMAIL, site=SITE):
    return JiraProvider(site, Secret(token), email=email, transport=fake.transport(), sleep=lambda s: None)


def test_whoami_and_project_listing(fake):
    p = provider(fake)
    assert p.whoami().login == fj.EMAIL
    projects, more = p.list_projects()
    assert [x.key for x in projects] == ["ABC", "NOEP"] and not more  # HID is not browsable
    assert [x.key for x in p.list_projects(q="billing")[0]] == ["ABC"]


def test_project_schema_is_parsed(fake):
    p = provider(fake)
    project, types = p.get_project("ABC")
    assert project.id == "10100"
    assert {(t.name, t.hierarchy_level, t.subtask) for t in types} >= {
        ("Epic", 1, False),
        ("Story", 0, False),
        ("Sub-task", -1, True),
    }
    fields = {f.id: f for f in p.fields()}
    assert fields[fj.STORY_POINTS].custom_type == "com.pyxis.greenhopper.jira:jsw-story-points"
    assert fields[fj.SPRINT].schema_type == "array" and not fields["summary"].custom
    statuses = p.statuses("ABC")
    assert [(s.name, s.category) for s in statuses["10004"]][-1] == ("Verified", "done")
    assert {lt.outward for lt in p.link_types()} == {"blocks", "relates to"}


def test_boards_and_sprints_skip_kanban(fake):
    p = provider(fake)
    boards = p.boards("ABC")
    assert [(b.id, b.type) for b in boards] == [("1", "scrum"), ("2", "kanban")]
    assert [(s.id, s.state) for s in p.sprints(boards[0])] == [("11", "closed"), ("12", "active")]
    assert p.sprints(boards[1]) == []  # never asks a kanban board for sprints
    assert ("GET", "/rest/agile/1.0/board/2/sprint") not in fake.calls
    assert p.boards("NOEP") == []


def test_permissions_distinguish_read_only_tokens(fake):
    assert all(provider(fake).my_permissions("ABC").values())
    ro = provider(fake, fj.READONLY_TOKEN, "reader@acme.example").my_permissions("ABC")
    assert ro["BROWSE_PROJECTS"] and not any(v for k, v in ro.items() if k != "BROWSE_PROJECTS")


def test_issue_search_pages_with_next_page_token(fake):
    p = provider(fake)
    first = p.search_issues("project = ABC ORDER BY key", ["summary"], max_results=2)
    assert [i["key"] for i in first.issues] == ["ABC-1", "ABC-2"] and first.next_page_token
    assert set(first.issues[0]["fields"]) == {"summary"}
    keys = [i["key"] for i in p.iter_issues("project = ABC", ["summary"])]
    assert keys == ["ABC-1", "ABC-2", "ABC-3", "ABC-4", "ABC-5"]


def test_data_center_uses_bearer_and_v2(fake):
    p = JiraProvider(
        "https://jira.corp.example.com", Secret(fj.DC_PAT), transport=fake.transport(), sleep=lambda s: None
    )
    assert p.whoami().login == "dc-bot"
    assert [x.key for x in p.list_projects()[0]] == ["ABC"]
    page = p.search_issues("project = ABC", ["summary"], max_results=3)
    assert len(page.issues) == 3 and page.next_page_token == "3"
    assert all(path.startswith(("/rest/api/2", "/rest/agile")) for _, path in fake.calls)


def test_errors_are_mapped(fake):
    with pytest.raises(CredentialInvalid) as exc:
        provider(fake, fj.BAD_TOKEN).whoami()
    assert fj.BAD_TOKEN not in str(exc.value)
    with pytest.raises(CredentialInvalid):
        provider(fake, email="someone-else@acme.example").whoami()  # token belongs to another account
    with pytest.raises(NotFoundOrNoAccess):
        provider(fake).get_project("HID")


def test_retries_rate_limit_and_redirects(fake):
    fake.failures = ["timeout", "500"]
    assert provider(fake).whoami().login == fj.EMAIL and len(fake.calls) == 3
    fake.calls.clear()
    fake.failures = ["500"] * 4
    with pytest.raises(ProviderUnavailable, match="Jira did not respond successfully after 3 attempts"):
        provider(fake).whoami()
    assert len(fake.calls) == 3
    fake.calls.clear()
    fake.failures = ["ratelimit"]
    with pytest.raises(RateLimited) as exc:
        provider(fake).whoami()
    assert exc.value.retry_after == 42 and len(fake.calls) == 1
    fake.failures = ["redirect"]
    with pytest.raises(ProviderError, match="redirect"):
        provider(fake).whoami()


def test_site_outside_allowlist_is_refused_before_any_call(fake):
    for site in ("https://169.254.169.254", "https://atlassian.net.evil.com", "http://acme.atlassian.net"):
        with pytest.raises(EgressDenied):
            provider(fake, site=site)
    assert fake.calls == []
