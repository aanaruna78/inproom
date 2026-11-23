"""INPROOM-84: workspace and project APIs against real PostgreSQL (story INPROOM-18 AC 1, 2, 3, 5, 6, 7)."""

import uuid

import pytest
from sqlalchemy import text

from app.config import get_settings
from tests.auth_helpers import bearer

pytestmark = pytest.mark.integration


@pytest.fixture
def api(db_client, tokens):
    """Small DSL around the TestClient: one random tenant per test unless told otherwise."""

    class Api:
        client = db_client

        def __init__(self):
            self.tenant = uuid.uuid4()

        def h(self, user="alice", roles=("tenant_admin",), tenant=None, **extra):
            token = tokens.mint(tenant_id=tenant or self.tenant, user_id=user, roles=list(roles))
            return bearer(token) | extra

        def workspace(self, slug="main", **kw):
            r = self.client.post(
                "/v1/workspaces", json={"slug": slug, "name": slug.title()}, headers=self.h(**kw)
            )
            assert r.status_code == 201, r.text
            return r.json()

        def project(self, ws_id, key="DEMO", mode="NEW", **kw):
            r = self.client.post(
                f"/v1/workspaces/{ws_id}/projects",
                json={"key": key, "name": f"Project {key}", "mode": mode},
                headers=self.h(**kw),
            )
            assert r.status_code == 201, r.text
            return r.json()

    return Api()


def q(owner_engine, sql, **params):
    with owner_engine.connect() as conn:
        return conn.execute(text(sql), params).scalar()


# ---------------------------------------------------------------- workspaces


def test_create_workspace_writes_row_audit_and_outbox(api, owner_engine):
    r = api.client.post("/v1/workspaces", json={"slug": "platform", "name": "Platform"}, headers=api.h())
    assert r.status_code == 201, r.text
    ws = r.json()
    assert ws["slug"] == "platform" and ws["created_by"] == "alice"
    t = api.tenant
    assert q(owner_engine, "SELECT count(*) FROM tenants WHERE id=:t", t=t) == 1  # auto-provisioned
    assert (
        q(
            owner_engine,
            "SELECT count(*) FROM audit_events WHERE tenant_id=:t AND action='workspace.create' "
            "AND outcome='SUCCESS' AND target_id=:w",
            t=t,
            w=ws["id"],
        )
        == 1
    )
    assert (
        q(
            owner_engine,
            "SELECT count(*) FROM outbox_events WHERE tenant_id=:t AND type='workspace.created'",
            t=t,
        )
        == 1
    )


def test_duplicate_workspace_slug_is_409_but_allowed_in_another_tenant(api):
    api.workspace("shared")
    dup = api.client.post("/v1/workspaces", json={"slug": "shared", "name": "x"}, headers=api.h())
    assert dup.status_code == 409 and dup.json()["code"] == "WORKSPACE_SLUG_EXISTS"
    other = api.client.post(
        "/v1/workspaces", json={"slug": "shared", "name": "x"}, headers=api.h(tenant=uuid.uuid4())
    )
    assert other.status_code == 201


def test_workspace_create_requires_permission_and_audits_denial(api, owner_engine):
    api.workspace("seed")  # provisions the tenant
    r = api.client.post(
        "/v1/workspaces", json={"slug": "nope", "name": "x"}, headers=api.h("dave", ("developer",))
    )
    assert r.status_code == 403 and r.json()["code"] == "FORBIDDEN"
    assert (
        q(
            owner_engine,
            "SELECT count(*) FROM audit_events WHERE tenant_id=:t AND actor_id='dave' "
            "AND outcome='DENIED' AND action='tenant.workspace.create'",
            t=api.tenant,
        )
        == 1
    )


@pytest.mark.parametrize("slug", ["A-upper", "x", "-lead", "has space", "a" * 41])
def test_invalid_workspace_slug_is_422(api, slug):
    r = api.client.post("/v1/workspaces", json={"slug": slug, "name": "x"}, headers=api.h())
    assert r.status_code == 422 and r.json()["code"] == "VALIDATION_ERROR"


def test_list_workspaces_is_tenant_scoped_and_paginated(api):
    for slug in ("ws-a", "ws-b", "ws-c"):
        api.workspace(slug)
    other = uuid.uuid4()
    api.workspace("foreign", tenant=other)
    first = api.client.get("/v1/workspaces?limit=2", headers=api.h("dave", ("developer",))).json()
    assert [w["slug"] for w in first["items"]] == ["ws-a", "ws-b"] and first["next_cursor"]
    second = api.client.get(f"/v1/workspaces?limit=2&cursor={first['next_cursor']}", headers=api.h()).json()
    assert [w["slug"] for w in second["items"]] == ["ws-c"] and second["next_cursor"] is None


def test_invalid_cursor_is_400(api):
    r = api.client.get("/v1/workspaces?cursor=not-a-cursor", headers=api.h())
    assert r.status_code == 400 and r.json()["code"] == "INVALID_CURSOR"


def test_workspace_create_is_idempotent(api, owner_engine):
    h = api.h(**{"Idempotency-Key": "ws-1"})
    a = api.client.post("/v1/workspaces", json={"slug": "idem", "name": "Idem"}, headers=h)
    b = api.client.post("/v1/workspaces", json={"slug": "idem", "name": "Idem"}, headers=h)
    assert a.status_code == b.status_code == 201 and a.json() == b.json()
    assert b.headers["Idempotent-Replayed"] == "true"
    assert q(owner_engine, "SELECT count(*) FROM workspaces WHERE tenant_id=:t", t=api.tenant) == 1


# ---------------------------------------------------------------- projects: create


def test_create_project_atomically_writes_owner_settings_audit_outbox(api, owner_engine):
    ws = api.workspace()
    r = api.client.post(
        f"/v1/workspaces/{ws['id']}/projects",
        json={"key": "INPROOM", "name": "INPROOM platform", "mode": "NEW", "process_package": "sdlc@1"},
        headers=api.h(),
    )
    assert r.status_code == 201, r.text
    p = r.json()
    assert r.headers["ETag"] == '"1"'
    assert p["status"] == "ACTIVE" and p["owner_id"] == "alice" and p["version"] == 1
    assert p["my_roles"] == ["project_admin"]
    # Settings schema v2 defaults (INPROOM-102 replaced the v1 shape; production actions are off).
    assert p["settings"] == {
        "schema_version": 2,
        "process_package": "sdlc@1",
        "write_mode": "read_only",
        "model_policy": "tenant_default",
        "approvals": {
            "required_scopes": ["plan"],
            "min_approvers": 1,
            "expiry_hours": 72,
            "self_approval": False,
        },
        "sources": {
            "allowed_kinds": ["git", "jira", "confluence", "upload"],
            "max_classification": "internal",
            "approve_new_sources": True,
        },
        "production": {"actions_enabled": False, "environments": [], "enabled_by": None, "enabled_at": None},
    }
    pid, t = p["id"], api.tenant
    assert (
        q(owner_engine, "SELECT role FROM project_members WHERE project_id=:p AND user_id='alice'", p=pid)
        == "project_admin"
    )
    assert q(
        owner_engine,
        "SELECT content_sha256 FROM project_settings_versions WHERE project_id=:p AND version=1",
        p=pid,
    ).startswith("sha256:")
    assert (
        q(
            owner_engine,
            "SELECT count(*) FROM audit_events WHERE project_id=:p AND action='project.create'",
            p=pid,
        )
        == 1
    )
    assert (
        q(
            owner_engine,
            "SELECT count(*) FROM outbox_events WHERE tenant_id=:t AND type='project.created' "
            "AND payload->>'id'=:p",
            t=t,
            p=pid,
        )
        == 1
    )


def test_migration_project_starts_pending_sources(api):
    ws = api.workspace()
    assert api.project(ws["id"], "LEGACY", mode="MIGRATION")["status"] == "PENDING_SOURCES"


def test_duplicate_project_key_is_409_and_writes_nothing(api, owner_engine):
    ws = api.workspace()
    api.project(ws["id"], "DUP")
    r = api.client.post(
        f"/v1/workspaces/{ws['id']}/projects", json={"key": "DUP", "name": "Again"}, headers=api.h()
    )
    assert r.status_code == 409 and r.json()["code"] == "PROJECT_KEY_EXISTS"
    assert q(owner_engine, "SELECT count(*) FROM projects WHERE tenant_id=:t", t=api.tenant) == 1
    assert (
        q(
            owner_engine,
            "SELECT count(*) FROM audit_events WHERE tenant_id=:t AND action='project.create'",
            t=api.tenant,
        )
        == 1
    )


def test_project_key_unique_per_tenant_not_globally(api):
    api.project(api.workspace()["id"], "SAME")
    other = uuid.uuid4()
    ws_other = api.workspace(tenant=other)
    api.project(ws_other["id"], "SAME", tenant=other)


def test_same_idempotency_key_creates_one_project(api, owner_engine):
    ws = api.workspace()
    h = api.h(**{"Idempotency-Key": "proj-create-1"})
    body = {"key": "IDEM", "name": "Idempotent"}
    a = api.client.post(f"/v1/workspaces/{ws['id']}/projects", json=body, headers=h)
    b = api.client.post(f"/v1/workspaces/{ws['id']}/projects", json=body, headers=h)
    assert a.status_code == b.status_code == 201 and a.json()["id"] == b.json()["id"]
    assert b.headers["Idempotent-Replayed"] == "true" and b.headers["ETag"] == '"1"'
    assert q(owner_engine, "SELECT count(*) FROM projects WHERE tenant_id=:t", t=api.tenant) == 1


def test_create_project_without_permission_is_403_and_audited(api, owner_engine):
    ws = api.workspace()
    r = api.client.post(
        f"/v1/workspaces/{ws['id']}/projects",
        json={"key": "NOPE", "name": "Nope"},
        headers=api.h("dave", ("developer",)),
    )
    assert r.status_code == 403
    assert (
        q(
            owner_engine,
            "SELECT count(*) FROM audit_events WHERE tenant_id=:t AND actor_id='dave' AND outcome='DENIED' "
            "AND action='workspace.project.create'",
            t=api.tenant,
        )
        == 1
    )
    assert q(owner_engine, "SELECT count(*) FROM projects WHERE tenant_id=:t", t=api.tenant) == 0


def test_create_project_in_unknown_or_foreign_workspace_is_404(api):
    foreign = api.workspace(tenant=uuid.uuid4())
    for ws_id in (foreign["id"], str(uuid.uuid4())):
        r = api.client.post(
            f"/v1/workspaces/{ws_id}/projects", json={"key": "X1", "name": "Xxx"}, headers=api.h()
        )
        assert r.status_code == 404 and r.json()["code"] == "NOT_FOUND"


@pytest.mark.parametrize(
    "body",
    [
        {"key": "lower", "name": "Name"},
        {"key": "A", "name": "Name"},
        {"key": "TOOLONGKEY1", "name": "Name"},
        {"key": "OK", "name": "ab"},
        {"key": "OK", "name": "Name", "mode": "OTHER"},
    ],
)
def test_invalid_project_payload_is_422(api, body):
    ws = api.workspace()
    r = api.client.post(f"/v1/workspaces/{ws['id']}/projects", json=body, headers=api.h())
    assert r.status_code == 422


def test_project_limit_per_workspace(api, monkeypatch):
    monkeypatch.setenv("INPROOM_WORKSPACE_PROJECT_LIMIT", "2")
    get_settings.cache_clear()
    ws = api.workspace()
    api.project(ws["id"], "ONE")
    second = api.project(ws["id"], "TWO")
    r = api.client.post(
        f"/v1/workspaces/{ws['id']}/projects", json={"key": "THREE", "name": "Three"}, headers=api.h()
    )
    assert r.status_code == 422 and r.json()["code"] == "PROJECT_LIMIT_REACHED"
    # Archived projects do not count towards the limit.
    api.client.post(f"/v1/projects/{second['id']}/archive", headers=api.h())
    api.project(ws["id"], "THREE")


def test_failure_inside_create_transaction_persists_nothing(api, owner_engine, monkeypatch):
    from app.modules import projects as projects_module

    ws = api.workspace()

    def boom(*args, **kwargs):
        raise RuntimeError("audit store unavailable")

    monkeypatch.setattr(projects_module.audit, "record", boom)
    with pytest.raises(RuntimeError):
        api.client.post(
            f"/v1/workspaces/{ws['id']}/projects",
            json={"key": "ATOMIC", "name": "Atomic"},
            headers=api.h(**{"Idempotency-Key": "atomic-1"}),
        )
    for table in ("projects", "project_members", "project_settings_versions"):
        assert q(owner_engine, f"SELECT count(*) FROM {table} WHERE tenant_id=:t", t=api.tenant) == 0, table
    assert (
        q(
            owner_engine,
            "SELECT count(*) FROM outbox_events WHERE tenant_id=:t AND type='project.created'",
            t=api.tenant,
        )
        == 0
    )
    assert q(owner_engine, "SELECT count(*) FROM idempotency_keys WHERE tenant_id=:t", t=api.tenant) == 0


# ---------------------------------------------------------------- projects: read


def _add_member(owner_engine, tenant, project_id, user, role):
    with owner_engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO project_members (tenant_id, project_id, user_id, role, added_by) "
                "VALUES (:t, :p, :u, :r, 'test')"
            ),
            {"t": tenant, "p": project_id, "u": user, "r": role},
        )


def test_list_projects_admin_sees_all_members_see_theirs(api, owner_engine):
    ws = api.workspace()
    p1, _p2 = api.project(ws["id"], "PONE"), api.project(ws["id"], "PTWO")
    admin = api.client.get(f"/v1/workspaces/{ws['id']}/projects", headers=api.h()).json()
    assert [p["key"] for p in admin["items"]] == ["PONE", "PTWO"]
    dev = api.h("dave", ("developer",))
    assert api.client.get(f"/v1/workspaces/{ws['id']}/projects", headers=dev).json()["items"] == []
    _add_member(owner_engine, api.tenant, p1["id"], "dave", "developer")
    assert [
        p["key"] for p in api.client.get(f"/v1/workspaces/{ws['id']}/projects", headers=dev).json()["items"]
    ] == ["PONE"]


def test_list_projects_paginates(api):
    ws = api.workspace()
    for key in ("PA", "PB", "PC"):
        api.project(ws["id"], key)
    page = api.client.get(f"/v1/workspaces/{ws['id']}/projects?limit=2", headers=api.h()).json()
    rest = api.client.get(
        f"/v1/workspaces/{ws['id']}/projects?limit=2&cursor={page['next_cursor']}", headers=api.h()
    ).json()
    assert [p["key"] for p in page["items"] + rest["items"]] == ["PA", "PB", "PC"] and rest[
        "next_cursor"
    ] is None


def test_get_project_returns_settings_and_etag(api):
    p = api.project(api.workspace()["id"])
    r = api.client.get(f"/v1/projects/{p['id']}", headers=api.h())
    assert r.status_code == 200 and r.headers["ETag"] == '"1"'
    assert r.json()["settings"]["write_mode"] == "read_only"


def test_get_project_hidden_from_non_members_and_other_tenants(api):
    p = api.project(api.workspace()["id"])
    for headers in (api.h("dave", ("developer",)), api.h(tenant=uuid.uuid4())):
        r = api.client.get(f"/v1/projects/{p['id']}", headers=headers)
        assert r.status_code == 404 and r.json()["code"] == "NOT_FOUND"


# ---------------------------------------------------------------- projects: update / archive


def test_patch_requires_if_match(api):
    p = api.project(api.workspace()["id"])
    r = api.client.patch(f"/v1/projects/{p['id']}", json={"name": "Renamed"}, headers=api.h())
    assert r.status_code == 428 and r.json()["code"] == "PRECONDITION_REQUIRED"


def test_patch_with_stale_if_match_is_412_and_unchanged(api):
    p = api.project(api.workspace()["id"])
    ok = api.client.patch(
        f"/v1/projects/{p['id']}", json={"name": "Renamed once"}, headers=api.h(**{"If-Match": '"1"'})
    )
    assert ok.status_code == 200 and ok.headers["ETag"] == '"2"' and ok.json()["version"] == 2
    stale = api.client.patch(
        f"/v1/projects/{p['id']}", json={"name": "Lost update"}, headers=api.h(**{"If-Match": '"1"'})
    )
    assert stale.status_code == 412 and stale.json()["code"] == "PRECONDITION_FAILED"
    assert api.client.get(f"/v1/projects/{p['id']}", headers=api.h()).json()["name"] == "Renamed once"


def test_patch_is_audited(api, owner_engine):
    p = api.project(api.workspace()["id"])
    api.client.patch(
        f"/v1/projects/{p['id']}", json={"description": "New"}, headers=api.h(**{"If-Match": '"1"'})
    )
    assert (
        q(
            owner_engine,
            "SELECT count(*) FROM audit_events WHERE project_id=:p AND action='project.update'",
            p=p["id"],
        )
        == 1
    )


def test_member_without_project_admin_cannot_patch_or_archive(api, owner_engine):
    p = api.project(api.workspace()["id"])
    _add_member(owner_engine, api.tenant, p["id"], "dave", "developer")
    dev = api.h("dave", ("developer",), **{"If-Match": '"1"'})
    assert (
        api.client.patch(f"/v1/projects/{p['id']}", json={"name": "Hijack"}, headers=dev).status_code == 403
    )
    assert api.client.post(f"/v1/projects/{p['id']}/archive", headers=dev).status_code == 403


def test_project_admin_member_can_patch(api, owner_engine):
    p = api.project(api.workspace()["id"])
    _add_member(owner_engine, api.tenant, p["id"], "pam", "project_admin")
    r = api.client.patch(
        f"/v1/projects/{p['id']}",
        json={"name": "By pam"},
        headers=api.h("pam", ("project_admin",), **{"If-Match": '"1"'}),
    )
    assert r.status_code == 200 and r.json()["my_roles"] == ["project_admin"]


def test_archive_is_idempotent_and_blocks_updates(api, owner_engine):
    p = api.project(api.workspace()["id"])
    first = api.client.post(f"/v1/projects/{p['id']}/archive", headers=api.h())
    again = api.client.post(f"/v1/projects/{p['id']}/archive", headers=api.h())
    assert first.json()["status"] == again.json()["status"] == "ARCHIVED"
    assert first.json()["version"] == again.json()["version"] == 2
    assert (
        q(
            owner_engine,
            "SELECT count(*) FROM audit_events WHERE project_id=:p AND action='project.archive'",
            p=p["id"],
        )
        == 1
    )
    r = api.client.patch(
        f"/v1/projects/{p['id']}", json={"name": "Too late"}, headers=api.h(**{"If-Match": '"2"'})
    )
    assert r.status_code == 409 and r.json()["code"] == "PROJECT_ARCHIVED"
