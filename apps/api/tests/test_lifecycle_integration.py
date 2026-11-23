"""INPROOM-103 (story INPROOM-21 AC 6, 7): project lifecycle state machine and archived read-only rules."""

import uuid

import pytest
from sqlalchemy import text

from app.config import get_settings
from app.modules.projects import TRANSITIONS, allowed_actions
from tests.auth_helpers import bearer

pytestmark = pytest.mark.integration


@pytest.fixture
def env(db_client, tokens, owner_engine):
    tenant = uuid.uuid4()

    class Env:
        client = db_client

        def h(self, user="alice", roles=("tenant_admin",), **extra):
            return bearer(tokens.mint(tenant_id=tenant, user_id=user, roles=list(roles))) | extra

        def project(self, key, mode="NEW"):
            r = db_client.post(
                f"/v1/workspaces/{self.ws}/projects",
                json={"key": key, "name": f"P {key}", "mode": mode},
                headers=self.h(),
            )
            assert r.status_code == 201, r.text
            return r.json()

        def act(self, pid, action, reason=None, **kw):
            body = {"action": action} | ({"reason": reason} if reason else {})
            return db_client.post(f"/v1/projects/{pid}/lifecycle", json=body, headers=self.h(**kw))

        def count(self, sql, **params):
            with owner_engine.connect() as conn:
                return conn.execute(text(sql), {"t": tenant} | params).scalar()

        def trail(self, pid):
            audits = self.count(
                "SELECT count(*) FROM audit_events WHERE tenant_id=:t AND project_id=:p "
                "AND outcome='SUCCESS' "
                "AND action IN ('project.activate','project.archive','project.restore')",
                p=pid,
            )
            events = self.count(
                "SELECT count(*) FROM outbox_events WHERE tenant_id=:t AND type='project.status.changed' "
                "AND payload->>'project_id' = :p",
                p=pid,
            )
            return audits, events

    e = Env()
    e.tenant = tenant
    e.ws = db_client.post("/v1/workspaces", json={"slug": "main", "name": "Main"}, headers=e.h()).json()["id"]
    return e


def _project_in(env, status, key):
    if status == "PENDING_SOURCES":
        return env.project(key, mode="MIGRATION")
    p = env.project(key)
    if status == "ARCHIVED":
        assert env.act(p["id"], "archive").status_code == 200
    return p


CELLS = [
    (s, a) for s in ("PENDING_SOURCES", "ACTIVE", "ARCHIVED") for a in ("activate", "archive", "restore")
]


@pytest.mark.parametrize(("start", "action"), CELLS, ids=[f"{s}-{a}" for s, a in CELLS])
def test_every_cell_of_the_transition_table(env, start, action):
    p = _project_in(env, start, "L" + uuid.uuid4().hex[:6].upper())
    before = env.trail(p["id"])
    r = env.act(p["id"], action, reason="test")
    target = TRANSITIONS.get((start, action))
    if target is None:
        assert r.status_code == 409, r.text
        body = r.json()
        assert body["code"] == "INVALID_TRANSITION" and body["current_status"] == start
        assert body["allowed_actions"] == allowed_actions(start)
        assert env.trail(p["id"]) == before
        failed = env.count(
            "SELECT count(*) FROM audit_events WHERE tenant_id=:t AND trace_id=:tr AND outcome='FAILED'",
            tr=body["trace_id"],
        )
        assert failed == 1
    elif target == start:
        assert r.status_code == 200 and r.json()["status"] == start
        assert r.json()["version"] == p["version"] + (
            1 if start == "ARCHIVED" else 0
        )  # archived via API above
        assert env.trail(p["id"]) == before  # no-op writes nothing
    else:
        assert r.status_code == 200 and r.json()["status"] == target
        assert env.trail(p["id"]) == (before[0] + 1, before[1] + 1)


def test_allowed_actions_per_status():
    assert allowed_actions("PENDING_SOURCES") == ["activate", "archive"]
    assert allowed_actions("ACTIVE") == ["archive"]
    assert allowed_actions("ARCHIVED") == ["restore"]


def test_transition_bumps_version_and_records_reason(env):
    p = env.project("REASON")
    r = env.act(p["id"], "archive", reason="end of pilot")
    assert r.json()["version"] == 2 and r.headers["ETag"] == '"2"'
    payload = env.count(
        "SELECT payload::text FROM outbox_events WHERE tenant_id=:t AND type='project.status.changed'"
    )
    assert (
        '"reason": "end of pilot"' in payload
        and '"from": "ACTIVE"' in payload
        and '"to": "ARCHIVED"' in payload
    )


def test_archive_alias_behaves_like_lifecycle_archive(env):
    p = env.project("ALIAS")
    r = env.client.post(f"/v1/projects/{p['id']}/archive", headers=env.h())
    assert r.status_code == 200 and r.json()["status"] == "ARCHIVED"
    assert env.trail(p["id"]) == (1, 1)
    again = env.client.post(f"/v1/projects/{p['id']}/archive", headers=env.h())
    assert again.status_code == 200 and env.trail(p["id"]) == (1, 1)


def test_archived_project_is_read_only_but_access_can_be_revoked(env):
    p = env.project("RO")
    pid = p["id"]
    env.client.get("/v1/me", headers=env.h("dave", ()))
    assert (
        env.client.put(
            f"/v1/projects/{pid}/members/dave", json={"roles": ["viewer"]}, headers=env.h()
        ).status_code
        == 200
    )
    assert env.act(pid, "archive").status_code == 200
    writes = [
        env.client.patch(f"/v1/projects/{pid}", json={"name": "Nope"}, headers=env.h(**{"If-Match": '"2"'})),
        env.client.put(
            f"/v1/projects/{pid}/settings",
            json={"settings": {"write_mode": "draft_only"}},
            headers=env.h(**{"If-Match": '"1"'}),
        ),
        env.client.put(f"/v1/projects/{pid}/members/dave", json={"roles": ["tester"]}, headers=env.h()),
    ]
    assert [(w.status_code, w.json()["code"]) for w in writes] == [(409, "PROJECT_ARCHIVED")] * 3
    for path in ("", "/settings", "/settings/versions", "/members", "/permissions/me"):
        assert env.client.get(f"/v1/projects/{pid}{path}", headers=env.h()).status_code == 200, path
    assert env.client.delete(f"/v1/projects/{pid}/members/dave", headers=env.h()).status_code == 204


def test_restored_project_is_editable_again(env):
    p = env.project("BACK")
    env.act(p["id"], "archive")
    r = env.act(p["id"], "restore")
    assert r.status_code == 200 and r.json()["status"] == "ACTIVE"
    edit = env.client.patch(
        f"/v1/projects/{p['id']}",
        json={"name": "Back again"},
        headers=env.h(**{"If-Match": f'"{r.json()["version"]}"'}),
    )
    assert edit.status_code == 200


def test_restore_respects_the_workspace_project_limit(env, monkeypatch):
    first = env.project("CAP1")
    env.act(first["id"], "archive")
    monkeypatch.setenv("INPROOM_WORKSPACE_PROJECT_LIMIT", "1")
    get_settings.cache_clear()
    env.project("CAP2")  # archived projects don't count, so this fits
    r = env.act(first["id"], "restore")
    assert r.status_code == 422 and r.json()["code"] == "PROJECT_LIMIT_REACHED"
    assert env.client.get(f"/v1/projects/{first['id']}", headers=env.h()).json()["status"] == "ARCHIVED"


def test_only_project_admins_change_status(env):
    p = env.project("PERM")
    env.client.get("/v1/me", headers=env.h("dave", ()))
    env.client.put(f"/v1/projects/{p['id']}/members/dave", json={"roles": ["developer"]}, headers=env.h())
    r = env.act(p["id"], "archive", user="dave", roles=())
    assert r.status_code == 403 and r.json()["required_permission"] == "project.archive"
    assert env.client.get(f"/v1/projects/{p['id']}", headers=env.h()).json()["status"] == "ACTIVE"


def test_unknown_action_is_a_validation_error(env):
    p = env.project("BAD")
    assert env.act(p["id"], "delete").status_code == 422
