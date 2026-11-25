"""INPROOM-131 (story INPROOM-23 AC 1-7): Jira connections, selection, schema discovery and mapping."""

import uuid

import pytest
from sqlalchemy import text

from tests.auth_helpers import bearer
from tests.fakes import jira as fj

pytestmark = pytest.mark.integration
SITE = "https://acme.atlassian.net"


@pytest.fixture
def env(db_client, tokens, owner_engine, fake_jira):
    tenant = uuid.uuid4()

    class Env:
        client = db_client
        jira = fake_jira

        def h(self, user="alice", roles=("tenant_admin",), **extra):
            return bearer(tokens.mint(tenant_id=tenant, user_id=user, roles=list(roles))) | extra

        def connect(self, token=fj.VALID_TOKEN, email=fj.EMAIL, name="Jira", site=SITE):
            body = {"provider": "jira", "display_name": name, "base_url": site, "token": token}
            if email:
                body["email"] = email
            return db_client.post("/v1/connections", json=body, headers=self.h())

        def select(self, key="ABC", cid=None):
            return db_client.put(
                f"/v1/projects/{self.pid}/sources/jira/{cid or self.cid}:{key}", headers=self.h()
            )

        def src(self, path=""):
            return f"/v1/projects/{self.pid}/sources/jira/{self.sid}{path}"

        def q(self, sql, **params):
            with owner_engine.connect() as conn:
                return conn.execute(text(sql), {"t": tenant} | params).scalar()

    e = Env()
    e.tenant = tenant
    ws = db_client.post("/v1/workspaces", json={"slug": "jira", "name": "Jira"}, headers=e.h()).json()
    e.pid = db_client.post(
        f"/v1/workspaces/{ws['id']}/projects", json={"key": "JIR", "name": "Jira"}, headers=e.h()
    ).json()["id"]
    return e


@pytest.fixture
def selected(env):
    r = env.connect()
    assert r.status_code == 201, r.text
    env.cid = r.json()["id"]
    r = env.select()
    assert r.status_code == 200, r.text
    env.sid = r.json()["id"]
    return env


def _codes(source):
    return sorted(w["code"] for w in source["warnings"])


# ---------------------------------------------------------------- AC 1, 3: connect


def test_connect_jira_cloud_shows_account_and_fingerprint_only(env):
    r = env.connect()
    assert r.status_code == 201, r.text
    body = r.json()
    assert (body["provider"], body["kind"], body["auth_type"], body["status"]) == (
        "jira",
        "jira",
        "basic_token",
        "ACTIVE",
    )
    assert body["account_login"] == fj.EMAIL and body["base_url"] == SITE
    assert body["credential_fingerprint"] == "…CD34" and fj.VALID_TOKEN not in r.text
    assert env.q("SELECT count(*) FROM connection_secrets WHERE tenant_id=:t") == 1


@pytest.mark.parametrize(
    ("kwargs", "status", "code"),
    [
        ({"token": fj.BAD_TOKEN}, 422, "CREDENTIAL_INVALID"),
        ({"email": "someone@else.example"}, 422, "CREDENTIAL_INVALID"),
        ({"site": "https://atlassian.net.evil.com"}, 422, "BASE_URL_NOT_ALLOWED"),
        ({"site": "https://169.254.169.254"}, 422, "BASE_URL_NOT_ALLOWED"),
    ],
)
def test_rejected_connections_store_nothing(env, kwargs, status, code):
    r = env.connect(**kwargs)
    assert r.status_code == status and r.json()["code"] == code
    assert env.q("SELECT count(*) FROM source_connections WHERE tenant_id=:t") == 0
    assert env.q("SELECT count(*) FROM connection_secrets WHERE tenant_id=:t") == 0


def test_data_center_pat_connects_without_email(env):
    r = env.connect(token=fj.DC_PAT, email=None, site="https://jira.corp.example.com")
    assert r.status_code == 201 and r.json()["auth_type"] == "pat" and r.json()["account_login"] == "dc-bot"


# ---------------------------------------------------------------- AC 2, 5: select + discover


def test_select_discovers_schema_mapping_and_capabilities(selected):
    env = selected
    source = env.client.get(f"/v1/projects/{env.pid}/sources/jira", headers=env.h()).json()[0]
    assert (source["jira_key"], source["jira_project_id"], source["status"], source["snapshot_version"]) == (
        "ABC",
        "10100",
        "ACTIVE",
        1,
    )
    assert source["mapping"]["fields"]["story_points"] == {"field_id": fj.STORY_POINTS, "source": "auto"}
    assert source["mapping"]["issue_types"]["10000"] == "epic"
    assert source["mapping"]["statuses"]["10003"] == "done"
    # default project write_mode is read_only, and this token can write: least privilege warning
    assert source["capabilities"]["write_capable"] and _codes(source) == ["EXCESS_PRIVILEGE"]
    schema = env.client.get(env.src("/schema"), headers=env.h()).json()
    assert schema["version"] == 1 and schema["diff"] == {}
    assert {b["type"] for b in schema["schema"]["boards"]} == {"scrum", "kanban"}
    assert [s["name"] for s in schema["schema"]["sprints"]] == ["Sprint 11", "Sprint 12"]
    assert {lt["outward"] for lt in schema["schema"]["link_types"]} == {"blocks", "relates to"}
    # selecting again is a no-op: no second snapshot, no second event
    assert env.select().json()["version"] == source["version"]
    assert env.q("SELECT count(*) FROM jira_schema_snapshots WHERE tenant_id=:t") == 1
    assert env.q("SELECT count(*) FROM outbox_events WHERE tenant_id=:t AND type LIKE 'source.jira.%'") == 1


def test_read_only_token_and_missing_pieces(env):
    env.cid = env.connect(token=fj.READONLY_TOKEN, email="reader@acme.example").json()["id"]
    abc = env.select("ABC").json()
    assert abc["capabilities"]["write_capable"] is False and _codes(abc) == []
    noep = env.select("NOEP").json()
    assert noep["status"] == "ACTIVE" and _codes(noep) == ["NO_BOARD", "NO_EPIC_TYPE"]
    hidden = env.select("HID")
    assert hidden.status_code == 422 and hidden.json()["code"] == "JIRA_PROJECT_NOT_ACCESSIBLE"
    assert env.q("SELECT count(*) FROM project_jira_sources WHERE tenant_id=:t") == 2


def test_provider_failures_write_nothing(env):
    env.cid = env.connect().json()["id"]
    env.jira.failures = ["ratelimit"]
    r = env.select()
    assert r.status_code == 503 and r.headers["retry-after"] == "42"
    env.jira.failures = ["timeout"] * 3
    assert env.select().status_code == 502
    assert env.q("SELECT count(*) FROM project_jira_sources WHERE tenant_id=:t") == 0


def test_policy_kind_and_provider_guards(env):
    env.cid = env.connect().json()["id"]
    git_route = env.client.put(
        f"/v1/projects/{env.pid}/sources/repositories/{env.cid}/1", json={"branches": []}, headers=env.h()
    )
    assert git_route.status_code == 409 and git_route.json()["code"] == "CONNECTION_PROVIDER_MISMATCH"
    env.client.put(
        f"/v1/projects/{env.pid}/settings",
        json={"settings": {"sources": {"allowed_kinds": ["git"]}}},
        headers=env.h(**{"If-Match": '"1"'}),
    )
    blocked = env.select()
    assert blocked.status_code == 409 and blocked.json()["code"] == "SOURCE_KIND_NOT_ALLOWED"


# ---------------------------------------------------------------- AC 6: rediscovery + mapping


def test_mapping_overrides_need_if_match_and_known_targets(selected):
    env = selected
    body = {"fields": {"acceptance_criteria": fj.TEAM}}
    r0 = env.client.put(env.src("/mapping"), json=body, headers=env.h())
    assert r0.status_code == 428, r0.text
    stale = env.client.put(env.src("/mapping"), json=body, headers=env.h(**{"If-Match": '"7"'}))
    assert stale.status_code == 412
    unknown = env.client.put(
        env.src("/mapping"),
        json={"fields": {"story_points": "customfield_404"}},
        headers=env.h(**{"If-Match": '"1"'}),
    )
    assert unknown.status_code == 422 and unknown.json()["code"] == "MAPPING_TARGET_UNKNOWN"
    ok = env.client.put(env.src("/mapping"), json=body, headers=env.h(**{"If-Match": '"1"'}))
    assert ok.status_code == 200 and ok.headers["etag"] == '"2"'
    assert ok.json()["mapping"]["fields"]["acceptance_criteria"] == {
        "field_id": fj.TEAM,
        "source": "override",
    }


def test_rediscovery_stores_changes_once_with_diff(selected):
    env = selected
    same = env.client.post(env.src("/discover"), headers=env.h()).json()
    assert same["changed"] is False and same["snapshot_version"] == 1
    env.client.put(
        env.src("/mapping"),
        json={"fields": {"acceptance_criteria": fj.TEAM}},
        headers=env.h(**{"If-Match": '"1"'}),
    )
    env.jira.fields = [f for f in env.jira.fields if f["id"] != fj.TEAM]  # the overridden field is deleted
    changed = env.client.post(env.src("/discover"), headers=env.h()).json()
    assert changed["changed"] is True and changed["snapshot_version"] == 2
    assert changed["diff"] == {"fields": {"removed": [{"id": fj.TEAM, "name": "Team"}]}}
    assert "OVERRIDE_TARGET_REMOVED" in _codes(changed["source"])
    assert env.client.get(env.src("/schema?version=1"), headers=env.h()).json()["version"] == 1
    events = "SELECT count(*) FROM outbox_events WHERE tenant_id=:t AND type='source.jira.schema.changed'"
    assert env.q(events) == 1


# ---------------------------------------------------------------- AC 7: health, revoke


def test_lost_access_pauses_the_project_and_recovery_restores_it(selected):
    env = selected
    env.jira.revoke(fj.VALID_TOKEN, "ABC")
    check = env.client.post(f"/v1/connections/{env.cid}/check", headers=env.h()).json()
    assert check["status"] == "DEGRADED"
    assert [(i["code"], i.get("project")) for i in check["health"]["issues"]] == [
        ("JIRA_PROJECT_NOT_ACCESSIBLE", "ABC")
    ]
    assert (
        env.client.get(f"/v1/projects/{env.pid}/sources/jira", headers=env.h()).json()[0]["status"]
        == "PAUSED"
    )
    env.jira.users[fj.VALID_TOKEN].access["ABC"] = set(fj.BROWSE_ONLY)
    check = env.client.post(f"/v1/connections/{env.cid}/check", headers=env.h()).json()
    assert check["status"] == "ACTIVE"
    assert (
        env.client.get(f"/v1/projects/{env.pid}/sources/jira", headers=env.h()).json()[0]["status"]
        == "ACTIVE"
    )


def test_revoke_pauses_jira_sources(selected):
    env = selected
    assert env.client.post(f"/v1/connections/{env.cid}/revoke", headers=env.h()).json()["status"] == "REVOKED"
    source = env.client.get(f"/v1/projects/{env.pid}/sources/jira", headers=env.h()).json()[0]
    assert source["status"] == "PAUSED" and source["status_reason"] == "connection revoked"
    assert env.select().status_code == 409
    assert (
        env.client.delete(f"/v1/projects/{env.pid}/sources/jira/{env.cid}:ABC", headers=env.h()).status_code
        == 204
    )
    assert env.q("SELECT count(*) FROM jira_schema_snapshots WHERE tenant_id=:t") == 0


def test_health_check_in_flight_keeps_a_concurrent_exclusion(selected, monkeypatch):
    """INPROOM-149 (Jira): an exclusion committed while the health check talks to Jira must survive."""
    from app.connectors.jira import JiraProvider

    env = selected
    env.jira.revoke(fj.VALID_TOKEN, "ABC")
    env.client.post(f"/v1/connections/{env.cid}/check", headers=env.h())
    env.jira.users[fj.VALID_TOKEN].access["ABC"] = set(fj.BROWSE_ONLY)
    original, fired = JiraProvider.my_permissions, []

    def excluded_meanwhile(provider, key):
        result = original(provider, key)
        if not fired:
            fired.append(1)
            path = f"/v1/projects/{env.pid}/sources/jira/{env.sid}/exclude"
            assert env.client.post(path, headers=env.h()).status_code == 200
        return result

    monkeypatch.setattr(JiraProvider, "my_permissions", excluded_meanwhile)
    env.client.post(f"/v1/connections/{env.cid}/check", headers=env.h())
    assert fired and env.q("SELECT pause_cause FROM project_jira_sources WHERE tenant_id=:t") == "excluded"
