"""INPROOM-109 (story INPROOM-22 AC 1-5): Git connections and repository selection against a fake GitHub."""

import uuid

import pytest
from sqlalchemy import text

from tests.auth_helpers import bearer
from tests.fakes.github import BAD_TOKEN, FULL, OTHER_TOKEN, VALID_TOKEN

pytestmark = pytest.mark.integration


@pytest.fixture
def env(db_client, tokens, owner_engine, fake_github):
    tenant = uuid.uuid4()

    class Env:
        client = db_client
        gh = fake_github

        def h(self, user="alice", roles=("tenant_admin",), **extra):
            return bearer(tokens.mint(tenant_id=tenant, user_id=user, roles=list(roles))) | extra

        def connect(self, token=VALID_TOKEN, name="GitHub", **kw):
            body = {"display_name": name, "token": token} | kw
            return db_client.post("/v1/connections", json=body, headers=self.h())

        def select(self, repo, branches=None, user="alice", roles=("tenant_admin",)):
            return db_client.put(
                f"/v1/projects/{self.pid}/sources/repositories/{self.cid}/{repo}",
                json={"branches": branches or []},
                headers=self.h(user, roles),
            )

        def scope(self):
            return db_client.get(f"/v1/projects/{self.pid}/sources/scope", headers=self.h()).json()

        def q(self, sql, **params):
            with owner_engine.connect() as conn:
                return conn.execute(text(sql), {"t": tenant} | params).scalar()

    e = Env()
    e.tenant = tenant
    ws = db_client.post("/v1/workspaces", json={"slug": "src", "name": "Src"}, headers=e.h()).json()
    e.pid = db_client.post(
        f"/v1/workspaces/{ws['id']}/projects", json={"key": "SRC", "name": "Sources"}, headers=e.h()
    ).json()["id"]
    return e


@pytest.fixture
def connected(env):
    r = env.connect()
    assert r.status_code == 201, r.text
    env.cid = r.json()["id"]
    return env


# ---------------------------------------------------------------- AC 1: connect, nothing secret exposed


def test_connect_stores_only_ciphertext_and_shows_account_scopes_fingerprint(env, owner_engine):
    r = env.connect()
    assert r.status_code == 201, r.text
    body = r.json()
    assert (
        body["status"] == "ACTIVE"
        and body["account_login"] == "octo-bot"
        and body["granted_scopes"] == ["repo"]
    )
    assert body["credential_fingerprint"] == "…ab12" and "token" not in body
    assert VALID_TOKEN not in r.text
    with owner_engine.connect() as conn:
        dump = conn.execute(
            text(
                "SELECT string_agg(t::text, ' ') FROM (SELECT * FROM connection_secrets WHERE tenant_id=:t) t"
            ),
            {"t": env.tenant},
        ).scalar()
        assert dump and VALID_TOKEN not in dump and VALID_TOKEN.encode().hex() not in dump
        for table in ("source_connections", "audit_events", "outbox_events", "idempotency_keys"):
            row_text = conn.execute(
                text(
                    "SELECT coalesce(string_agg(t::text, ' '), '') "
                    f"FROM (SELECT * FROM {table} WHERE tenant_id=:t) t"
                ),
                {"t": env.tenant},
            ).scalar()
            assert VALID_TOKEN not in row_text, table
    listed = env.client.get("/v1/connections", headers=env.h("dave", ())).json()
    assert [c["display_name"] for c in listed] == ["GitHub"] and VALID_TOKEN not in str(listed)


def test_invalid_token_stores_nothing(env):
    r = env.connect(token=BAD_TOKEN)
    assert r.status_code == 422 and r.json()["code"] == "CREDENTIAL_INVALID"
    assert BAD_TOKEN not in r.text
    assert env.q("SELECT count(*) FROM source_connections WHERE tenant_id=:t") == 0


def test_disallowed_base_url_is_refused_without_calling_out(env):
    r = env.connect(base_url="https://169.254.169.254")
    assert r.status_code == 422 and r.json()["code"] == "BASE_URL_NOT_ALLOWED"
    assert env.gh.calls == []


def test_idempotent_connect_and_duplicate_name(env):
    h = env.h(**{"Idempotency-Key": "conn-1"})
    body = {"display_name": "GitHub", "token": VALID_TOKEN}
    first = env.client.post("/v1/connections", json=body, headers=h)
    again = env.client.post("/v1/connections", json=body, headers=h)
    assert first.status_code == again.status_code == 201 and first.json()["id"] == again.json()["id"]
    assert again.headers.get("Idempotent-Replayed") == "true"
    dup = env.connect()
    assert dup.status_code == 409 and dup.json()["code"] == "CONNECTION_NAME_EXISTS"
    assert env.q("SELECT count(*) FROM source_connections WHERE tenant_id=:t") == 1


def test_only_tenant_admin_manages_connections(env):
    r = env.client.post(
        "/v1/connections", json={"display_name": "Mine", "token": VALID_TOKEN}, headers=env.h("dave", ())
    )
    assert r.status_code == 403 and r.json()["required_permission"] == "tenant.connection.manage"


# ---------------------------------------------------------------- AC 3: provider failures


@pytest.mark.parametrize(
    ("failures", "status", "code"),
    [
        (["timeout", "timeout", "timeout"], 502, "PROVIDER_UNAVAILABLE"),
        (["500", "500", "500"], 502, "PROVIDER_UNAVAILABLE"),
        (["ratelimit"], 503, "PROVIDER_RATE_LIMITED"),
    ],
)
def test_provider_failures_are_actionable_and_write_nothing(env, failures, status, code):
    env.gh.failures = list(failures)
    r = env.connect()
    assert r.status_code == status and r.json()["code"] == code
    if code == "PROVIDER_RATE_LIMITED":
        assert r.headers["Retry-After"] == "42"
    assert env.q("SELECT count(*) FROM source_connections WHERE tenant_id=:t") == 0


def test_transient_failure_recovers_within_retry_budget(env):
    env.gh.failures = ["timeout", "500"]
    assert env.connect().status_code == 201


# ---------------------------------------------------------------- AC 2, 5: selection and scope


def test_repository_picker_marks_privacy_permissions_and_selection(connected):
    env = connected
    r = env.client.get(f"/v1/connections/{env.cid}/repositories?project_id={env.pid}", headers=env.h())
    assert r.status_code == 200, r.text
    items = {i["full_name"]: i for i in r.json()["items"]}
    assert set(items) == {"acme/api", "acme/web"}  # other/secret is invisible to this credential
    assert items["acme/api"]["private"] and items["acme/web"]["excess_privileges"] == ["push"]
    env.select("1")
    again = env.client.get(
        f"/v1/connections/{env.cid}/repositories?project_id={env.pid}&q=api", headers=env.h()
    )
    assert [(i["full_name"], i["selected"]) for i in again.json()["items"]] == [("acme/api", True)]


def test_only_selected_repositories_and_branches_are_in_scope(connected):
    env = connected
    r = env.select("1", ["main", "release/1.0"])
    assert r.status_code == 200, r.text
    assert r.json()["branches"] == ["main", "release/1.0"] and r.json()["private"] is True
    assert env.select("2").json()["branches"] == ["main"]  # empty => default branch
    scope = env.scope()
    assert {(s["full_name"], tuple(s["branches"])) for s in scope["repositories"]} == {
        ("acme/api", ("main", "release/1.0")),
        ("acme/web", ("main",)),
    }
    assert "develop" not in str(scope) and "other/secret" not in str(scope)


def test_selection_is_audited_once_and_unchanged_selection_is_a_noop(connected):
    env = connected
    env.select("1", ["main"])
    env.select("1", ["main"])
    env.select("1", ["main", "release/1.0"])
    actions = env.q(
        "SELECT string_agg(action, ',' ORDER BY occurred_at) FROM audit_events WHERE tenant_id=:t "
        "AND action LIKE 'source.repository.%'"
    )
    assert actions == "source.repository.selected,source.repository.updated"


def test_inaccessible_repository_and_missing_branch_are_rejected(connected):
    env = connected
    r = env.select("3")
    assert r.status_code == 422 and r.json()["code"] == "REPOSITORY_NOT_ACCESSIBLE"
    r = env.select("1", ["main", "nope"])
    assert (
        r.status_code == 422 and r.json()["code"] == "BRANCH_NOT_FOUND" and r.json()["branches"] == ["nope"]
    )
    r = env.select("1", ["../etc"])
    assert r.status_code == 422
    assert env.q("SELECT count(*) FROM project_repositories WHERE tenant_id=:t") == 0


def test_deselect_removes_from_scope(connected):
    env = connected
    env.select("1")
    path = f"/v1/projects/{env.pid}/sources/repositories/{env.cid}/1"
    assert env.client.delete(path, headers=env.h()).status_code == 204
    assert env.scope()["repositories"] == []
    gone = env.client.delete(path, headers=env.h())
    assert gone.status_code == 404 and gone.json()["code"] == "SOURCE_NOT_FOUND"


def test_settings_must_allow_git_sources(connected):
    env = connected
    r = env.client.put(
        f"/v1/projects/{env.pid}/settings",
        json={"settings": {"sources": {"allowed_kinds": ["jira", "confluence"]}}},
        headers=env.h(**{"If-Match": '"1"'}),
    )
    assert r.status_code == 200
    blocked = env.select("1")
    assert blocked.status_code == 409 and blocked.json()["code"] == "SOURCE_KIND_NOT_ALLOWED"


def test_project_roles_for_sources(connected):
    env = connected
    env.client.get("/v1/me", headers=env.h("vic", ()))
    env.client.put(f"/v1/projects/{env.pid}/members/vic", json={"roles": ["developer"]}, headers=env.h())
    r = env.select("1", user="vic", roles=())
    assert r.status_code == 403 and r.json()["required_permission"] == "project.source.manage"
    seen = env.client.get(f"/v1/projects/{env.pid}/sources/repositories", headers=env.h("vic", ()))
    assert seen.status_code == 200


# ---------------------------------------------------------------- health, rotate, revoke (AC 1, 4)


def test_check_detects_invalid_token_and_lost_access(connected):
    env = connected
    env.select("1", ["main", "release/1.0"])
    env.select("2")
    env.gh.tokens[VALID_TOKEN].access.pop(1)  # access to acme/api removed on GitHub
    r = env.client.post(f"/v1/connections/{env.cid}/check", headers=env.h())
    body = r.json()
    assert body["status"] == "DEGRADED"
    codes = {(i["code"], i.get("repository")) for i in body["health"]["issues"]}
    assert ("REPOSITORY_NOT_ACCESSIBLE", "acme/api") in codes and ("EXCESS_PRIVILEGE", "acme/web") in codes
    scope = env.scope()
    assert [s["full_name"] for s in scope["repositories"]] == ["acme/web"]
    [excluded] = scope["excluded"]
    assert {k: excluded[k] for k in ("full_name", "reason", "kind", "cause", "freshness")} == {
        "full_name": "acme/api",
        "reason": "the credential can no longer read this repository",
        "kind": "git",
        "cause": "access",  # INPROOM-145: a health check pause is an access pause
        "freshness": "paused",
    }
    env.gh.tokens[VALID_TOKEN].access[1] = dict(FULL)  # access restored
    assert env.client.post(f"/v1/connections/{env.cid}/check", headers=env.h()).json()["status"] == "ACTIVE"
    assert {s["full_name"] for s in env.scope()["repositories"]} == {"acme/api", "acme/web"}
    env.gh.tokens[VALID_TOKEN].valid = False
    assert env.client.post(f"/v1/connections/{env.cid}/check", headers=env.h()).json()["status"] == "INVALID"
    assert env.scope()["repositories"] == []


def test_outage_does_not_mark_a_good_credential_invalid(connected):
    env = connected
    env.gh.failures = ["500"] * 3
    body = env.client.post(f"/v1/connections/{env.cid}/check", headers=env.h()).json()
    assert body["status"] == "ACTIVE" and body["health"]["reachable"] is False


def test_rotate_keeps_old_credential_when_new_one_is_invalid(connected):
    env = connected
    path = f"/v1/connections/{env.cid}/credential"
    bad = env.client.put(path, json={"token": BAD_TOKEN}, headers=env.h())
    assert bad.status_code == 422
    assert (
        env.client.post(f"/v1/connections/{env.cid}/check", headers=env.h()).json()["account_login"]
        == "octo-bot"
    )
    good = env.client.put(path, json={"token": OTHER_TOKEN}, headers=env.h())
    assert good.status_code == 200 and good.json()["account_login"] == "octo-bot-2"
    assert good.json()["credential_fingerprint"] == "…cd34"


def test_revoke_destroys_secret_and_pauses_scope(connected):
    env = connected
    env.select("1")
    r = env.client.post(f"/v1/connections/{env.cid}/revoke", headers=env.h())
    assert (
        r.status_code == 200
        and r.json()["status"] == "REVOKED"
        and r.json()["credential_fingerprint"] is None
    )
    assert env.q("SELECT count(*) FROM connection_secrets WHERE tenant_id=:t") == 0
    assert env.scope()["repositories"] == []
    assert (
        env.client.post(f"/v1/connections/{env.cid}/revoke", headers=env.h()).status_code == 200
    )  # idempotent
    blocked = env.select("2")
    assert blocked.status_code == 409 and blocked.json()["code"] == "CONNECTION_NOT_ACTIVE"


def test_validation_errors_never_echo_the_submitted_token(env):
    short = "ghp_tooShortSecret1"  # 19 chars: rejected by validation
    r = env.client.post("/v1/connections", json={"display_name": "x", "token": short}, headers=env.h())
    assert r.status_code == 422 and short not in r.text
    assert all(set(e) <= {"type", "loc", "msg"} for e in r.json()["errors"])


def test_health_check_in_flight_keeps_a_concurrent_exclusion(connected, monkeypatch):
    """INPROOM-149 (GitHub): an exclusion committed while the health check talks to GitHub must survive."""
    from app.connectors.github import GitHubProvider

    env = connected
    env.select("1", ["main"])
    repo = env.client.get(f"/v1/projects/{env.pid}/sources/repositories", headers=env.h()).json()[0]
    env.gh.tokens[VALID_TOKEN].access.pop(1)  # lose access: the next check pauses it (cause: access)
    env.client.post(f"/v1/connections/{env.cid}/check", headers=env.h())
    env.gh.tokens[VALID_TOKEN].access[1] = dict(FULL)  # access restored: a check would resume it...
    original, fired = GitHubProvider.get_repository, []

    def excluded_meanwhile(provider, repo_id):
        result = original(provider, repo_id)
        if not fired:
            fired.append(1)  # ...but a project admin excludes it while the check is talking to GitHub
            path = f"/v1/projects/{env.pid}/sources/git/{repo['id']}/exclude"
            assert env.client.post(path, headers=env.h()).status_code == 200
        return result

    monkeypatch.setattr(GitHubProvider, "get_repository", excluded_meanwhile)
    env.client.post(f"/v1/connections/{env.cid}/check", headers=env.h())
    assert fired and env.q("SELECT pause_cause FROM project_repositories WHERE tenant_id=:t") == "excluded"
