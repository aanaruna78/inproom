"""INPROOM-108: GitHub adapter against the fake GitHub - mapping, retries, pagination, hooks."""

import pytest

from app.connectors.base import (
    CredentialInvalid,
    NotFoundOrNoAccess,
    ProviderError,
    ProviderUnavailable,
    RateLimited,
)
from app.connectors.egress import EgressDenied
from app.connectors.github import GitHubProvider
from app.connectors.vault import Secret
from tests.fakes.github import FULL, FakeGitHub

TOKEN = "github_pat_11FAKE0000000000000000000000000000000000000000000000000000000000000000"


@pytest.fixture
def gh():
    fake = FakeGitHub()
    fake.add_repo(1, "acme/api", private=True, branches={"main": "a" * 40, "release/1.0": "b" * 40})
    fake.add_repo(2, "acme/web")
    fake.add_repo(3, "other/secret", private=True)
    for i in range(60):
        fake.add_repo(100 + i, f"acme/svc-{i:02d}")
    fake.add_token(
        TOKEN, "octo-bot", {1: FULL, 2: FULL | {"push": True}} | {100 + i: FULL for i in range(60)}
    )
    return fake


def provider(fake, **kw):
    return GitHubProvider(
        "https://api.github.com", Secret(TOKEN), transport=fake.transport(), sleep=lambda s: None, **kw
    )


def test_whoami_reports_account_and_scopes(gh):
    account = provider(gh).whoami()
    assert account.login == "octo-bot" and account.scopes == ["repo"]


def test_repositories_are_paged_and_searchable(gh):
    p = provider(gh)
    first, more = p.list_repositories(page=1)
    assert len(first) == 50 and more
    second, more = p.list_repositories(page=2)
    assert len(second) == 12 and not more
    found, _ = p.list_repositories(q="SVC-5")
    assert [r.full_name for r in found] == [f"acme/svc-5{i}" for i in range(10)]
    assert "other/secret" not in {r.full_name for r in first + second}


def test_private_repository_permissions_and_excess_privilege(gh):
    p = provider(gh)
    repo = p.get_repository("1")
    assert repo.private and repo.can_read and repo.excess_privileges == []
    assert p.get_repository("2").excess_privileges == ["push"]
    with pytest.raises(NotFoundOrNoAccess):
        p.get_repository("3")  # exists, but the credential has no access


def test_branch_exists_handles_slashes(gh):
    p = provider(gh)
    assert p.branch_exists("1", "release/1.0") and p.branch_exists("1", "main")
    assert not p.branch_exists("1", "nope")


def test_invalid_credential(gh):
    gh.tokens[TOKEN].valid = False
    with pytest.raises(CredentialInvalid) as exc:
        provider(gh).whoami()
    assert TOKEN not in str(exc.value)


def test_transient_failures_are_retried_within_budget(gh):
    gh.failures = ["timeout", "500"]
    assert provider(gh).whoami().login == "octo-bot"
    assert len(gh.calls) == 3


def test_persistent_failure_stops_after_max_attempts(gh):
    gh.failures = ["timeout", "500", "timeout", "500"]
    with pytest.raises(ProviderUnavailable, match="3 attempts"):
        provider(gh).whoami()
    assert len(gh.calls) == 3


def test_rate_limit_is_not_retried(gh):
    gh.failures = ["ratelimit"]
    with pytest.raises(RateLimited) as exc:
        provider(gh).whoami()
    assert exc.value.retry_after == 42 and len(gh.calls) == 1


def test_redirects_are_never_followed(gh):
    gh.failures = ["redirect"]
    with pytest.raises(ProviderError, match="redirect"):
        provider(gh).whoami()
    assert len(gh.calls) == 1  # the metadata URL was never requested


def test_hooks_find_create_delete_idempotently(gh):
    gh.tokens[TOKEN].access[1] = FULL | {"admin": True}
    p = provider(gh)
    url = "https://inproom.example.com/v1/hooks/github/t/c"
    assert p.find_hook("1", url) is None
    hook_id = p.create_hook("1", url, "whsec", ["push"])
    assert p.find_hook("1", url) == hook_id
    p.delete_hook("1", hook_id)
    p.delete_hook("1", hook_id)  # second delete is a no-op
    assert gh.hooks[1] == []


def test_disallowed_base_url_is_refused_before_any_call(gh):
    with pytest.raises(EgressDenied):
        GitHubProvider("https://169.254.169.254", Secret(TOKEN), transport=gh.transport())
    assert gh.calls == []
