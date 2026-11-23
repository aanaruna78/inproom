"""INPROOM-102 (story INPROOM-21 AC 1-5): versioned project settings against the real API and database."""

import uuid

import pytest
from sqlalchemy import text

from tests.auth_helpers import bearer

pytestmark = pytest.mark.integration


@pytest.fixture
def env(db_client, tokens, owner_engine):
    tenant = uuid.uuid4()

    class Env:
        client = db_client

        def h(self, user="alice", roles=("tenant_admin",), version=None):
            headers = bearer(tokens.mint(tenant_id=tenant, user_id=user, roles=list(roles)))
            return headers | ({"If-Match": f'"{version}"'} if version is not None else {})

        def put(self, settings, version=1, note=None, **kw):
            body = {"settings": settings} | ({"change_note": note} if note else {})
            return db_client.put(
                f"/v1/projects/{self.pid}/settings", json=body, headers=self.h(version=version, **kw)
            )

        def get(self, path="", **kw):
            return db_client.get(f"/v1/projects/{self.pid}/settings{path}", headers=self.h(**kw))

        def count(self, sql, **params):
            with owner_engine.connect() as conn:
                return conn.execute(text(sql), {"t": tenant, "p": self.pid} | params).scalar()

        def versions(self):
            return self.count("SELECT count(*) FROM project_settings_versions WHERE project_id=:p")

        def events(self):
            return (
                self.count(
                    "SELECT count(*) FROM audit_events WHERE tenant_id=:t "
                    "AND action='project.settings.update' AND outcome='SUCCESS'"
                ),
                self.count(
                    "SELECT count(*) FROM outbox_events "
                    "WHERE tenant_id=:t AND type='project.settings.changed'"
                ),
            )

        def member(self, user, role):
            db_client.get("/v1/me", headers=self.h(user, ()))
            r = db_client.put(
                f"/v1/projects/{self.pid}/members/{user}", json={"roles": [role]}, headers=self.h()
            )
            assert r.status_code == 200, r.text

    e = Env()
    e.tenant = tenant
    ws = db_client.post("/v1/workspaces", json={"slug": "main", "name": "Main"}, headers=e.h()).json()
    e.pid = db_client.post(
        f"/v1/workspaces/{ws['id']}/projects", json={"key": "SET", "name": "Settings"}, headers=e.h()
    ).json()["id"]
    return e


def test_new_project_has_versioned_safe_defaults(env):
    r = env.get()
    assert r.status_code == 200 and r.headers["ETag"] == '"1"'
    body = r.json()
    assert body["version"] == 1 and body["schema_version"] == 2
    assert body["settings"]["write_mode"] == "read_only"
    assert body["settings"]["production"]["actions_enabled"] is False
    assert body["content_sha256"].startswith("sha256:") and body["changed_paths"] == []


def test_change_creates_new_immutable_version_with_one_audit_and_event(env, owner_engine):
    r = env.put(
        {"write_mode": "draft_only", "approvals": {"required_scopes": ["plan", "test"]}}, note="tighten QA"
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert r.headers["ETag"] == '"2"' and body["version"] == 2
    assert body["changed_paths"] == ["approvals.required_scopes", "write_mode"]
    assert body["change_note"] == "tighten QA" and body["created_by"] == "alice"
    assert env.get().json()["version"] == 2
    project = env.client.get(f"/v1/projects/{env.pid}", headers=env.h()).json()
    assert project["settings_version"] == 2 and project["settings"]["write_mode"] == "draft_only"
    assert env.versions() == 2 and env.events() == (1, 1)
    v1 = env.get("/versions/1").json()
    assert v1["settings"]["write_mode"] == "read_only"  # history is untouched


def test_identical_content_is_a_noop(env):
    same = {"approvals": {"required_scopes": ["plan", "plan"]}}  # canonicalises to the defaults
    r = env.put(same)
    assert r.status_code == 200 and r.json()["version"] == 1 and r.headers["ETag"] == '"1"'
    assert env.versions() == 1 and env.events() == (0, 0)


def test_retry_after_success_does_not_duplicate(env):
    first = env.put({"write_mode": "draft_only"})
    assert first.json()["version"] == 2
    stale_retry = env.put({"write_mode": "draft_only"})  # client retries with the old If-Match
    assert stale_retry.status_code == 412
    retry = env.put({"write_mode": "draft_only"}, version=2)
    assert retry.status_code == 200 and retry.json()["version"] == 2
    assert env.versions() == 2 and env.events() == (1, 1)


def test_if_match_is_required_and_checked(env):
    r = env.client.put(f"/v1/projects/{env.pid}/settings", json={"settings": {}}, headers=env.h())
    assert r.status_code == 428
    r = env.put({"write_mode": "draft_only"}, version=7)
    assert r.status_code == 412 and '"1"' in r.json()["detail"]
    assert env.versions() == 1 and env.events() == (0, 0)


@pytest.mark.parametrize(
    ("settings", "code", "loc"),
    [
        ({"process_package": "foo@1"}, "SETTINGS_INVALID", ["body", "settings", "process_package"]),
        (
            {"write_mode": "approved_writes", "approvals": {"required_scopes": []}},
            "SETTINGS_INVALID",
            ["body", "settings", "approvals", "required_scopes"],
        ),
        (
            {"production": {"actions_enabled": True}, "approvals": {"required_scopes": ["release"]}},
            "SETTINGS_INVALID",
            ["body", "settings", "production", "environments"],
        ),
        (
            {"approvals": {"self_approval": True}},
            "VALIDATION_ERROR",
            ["body", "settings", "approvals", "self_approval"],
        ),
        ({"surprise": 1}, "VALIDATION_ERROR", ["body", "settings", "surprise"]),
    ],
)
def test_invalid_settings_are_rejected_with_field_errors(env, settings, code, loc):
    r = env.put(settings)
    assert r.status_code == 422 and r.json()["code"] == code
    assert loc in [e["loc"] for e in r.json()["errors"]]
    assert env.versions() == 1 and env.events() == (0, 0)


PROD = {
    "production": {"actions_enabled": True, "environments": ["prod"]},
    "approvals": {"required_scopes": ["release"]},
}


def test_project_admin_cannot_enable_production(env):
    env.member("paula", "project_admin")
    r = env.put(PROD, user="paula", roles=())
    assert r.status_code == 403 and r.json()["required_permission"] == "project.production.enable"
    assert env.versions() == 1 and env.events() == (0, 0)
    denied = env.count(
        "SELECT count(*) FROM audit_events WHERE tenant_id=:t AND actor_id='paula' AND outcome='DENIED' "
        "AND action='project.production.enable'"
    )
    assert denied == 1


def test_tenant_admin_enables_production_and_server_stamps_it(env):
    forged = {
        **PROD,
        "production": {**PROD["production"], "enabled_by": "mallory", "enabled_at": "2000-01-01T00:00:00Z"},
    }
    r = env.put(forged, user="tina")
    assert r.status_code == 200, r.text
    prod = r.json()["settings"]["production"]
    assert prod["enabled_by"] == "tina" and not prod["enabled_at"].startswith("2000")


def test_project_admin_may_disable_but_not_change_enabled_production(env):
    assert env.put(PROD).status_code == 200  # tenant admin turns it on (v2)
    env.member("paula", "project_admin")
    change = {**PROD, "production": {"actions_enabled": True, "environments": ["prod", "eu-prod"]}}
    assert env.put(change, version=2, user="paula", roles=()).status_code == 403
    off = env.put({"approvals": {"required_scopes": ["release"]}}, version=2, user="paula", roles=())
    assert off.status_code == 200 and off.json()["settings"]["production"]["actions_enabled"] is False


def test_viewer_cannot_change_settings_but_can_read_history(env):
    env.member("vic", "viewer")
    r = env.put({"write_mode": "draft_only"}, user="vic", roles=())
    assert r.status_code == 403 and r.json()["required_permission"] == "project.settings.update"
    assert env.get(user="vic", roles=()).status_code == 200
    assert env.get("/versions", user="vic", roles=()).status_code == 200


def test_history_newest_first_with_paging_and_diff(env):
    for version, mode in ((1, "draft_only"), (2, "read_only"), (3, "draft_only")):
        assert env.put({"write_mode": mode}, version=version).status_code == 200
    page = env.get("/versions?limit=3").json()
    assert [v["version"] for v in page["items"]] == [4, 3, 2]
    rest = env.get(f"/versions?limit=3&cursor={page['next_cursor']}").json()
    assert [v["version"] for v in rest["items"]] == [1] and rest["next_cursor"] is None
    detail = env.get("/versions/3").json()
    assert detail["diff"] == [{"path": "write_mode", "old": "draft_only", "new": "read_only"}]
    assert env.get("/versions/1").json()["diff"] == []
    missing = env.get("/versions/99")
    assert missing.status_code == 404 and missing.json()["code"] == "SETTINGS_VERSION_NOT_FOUND"


def test_legacy_v1_row_is_read_as_v2_and_next_save_stores_v2(env, owner_engine):
    with owner_engine.begin() as conn:
        conn.execute(
            text(
                "UPDATE project_settings_versions SET schema_version=1, settings=CAST(:s AS jsonb) "
                "WHERE project_id=:p"
            ),
            {
                "p": env.pid,
                "s": '{"write_mode":"draft_only","approval_policy":"approval_required_for_external_writes",'
                '"model_policy":"tenant_default","process_package":"sdlc@1"}',
            },
        )
    body = env.get().json()
    assert body["schema_version"] == 1 and body["settings"]["schema_version"] == 2
    assert body["settings"]["write_mode"] == "draft_only"
    assert env.put(body["settings"]).json()["version"] == 1  # same content, no-op
    saved = env.put({"write_mode": "read_only"}).json()
    assert saved["version"] == 2 and saved["schema_version"] == 2


def test_archived_project_settings_are_read_only(env):
    assert env.client.post(f"/v1/projects/{env.pid}/archive", headers=env.h()).status_code == 200
    r = env.put({"write_mode": "draft_only"})
    assert r.status_code == 409 and r.json()["code"] == "PROJECT_ARCHIVED"
    assert env.get().status_code == 200


def test_create_rejects_unknown_process_package(db_client, tokens):
    h = bearer(tokens.mint(tenant_id=uuid.uuid4(), user_id="a", roles=["tenant_admin"]))
    ws = db_client.post("/v1/workspaces", json={"slug": "pkg-ws", "name": "W"}, headers=h).json()
    r = db_client.post(
        f"/v1/workspaces/{ws['id']}/projects",
        json={"key": "PKG", "name": "Pkg", "process_package": "foo@1"},
        headers=h,
    )
    assert r.status_code == 422
    ok = db_client.post(
        f"/v1/workspaces/{ws['id']}/projects",
        json={"key": "STL", "name": "Stlc", "process_package": "stlc@1"},
        headers=h,
    )
    assert ok.status_code == 201 and ok.json()["settings"]["process_package"] == "stlc@1"
