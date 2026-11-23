"""INPROOM-94 (story INPROOM-20 AC 2, 3, 5, 7): membership, directory and effective permissions."""

import threading
import uuid

import pytest
from sqlalchemy import text

from app.authz import policy
from tests.auth_helpers import bearer

pytestmark = pytest.mark.integration


@pytest.fixture
def env(db_client, tokens, owner_engine):
    tenant = uuid.uuid4()

    class Env:
        client = db_client

        def h(self, user="alice", roles=(), tenant_id=None, **extra):
            token = tokens.mint(
                tenant_id=tenant_id or tenant, user_id=user, roles=list(roles), email=f"{user}@example.test"
            )
            return bearer(token) | extra

        def signin(self, user, tenant_id=None):
            r = db_client.get("/v1/me", headers=self.h(user, tenant_id=tenant_id))
            assert r.status_code == 200, r.text

        def put(self, user, *roles, by=None):
            return db_client.put(
                f"/v1/projects/{self.pid}/members/{user}",
                json={"roles": list(roles)},
                headers=by or self.admin,
            )

        def delete(self, user, by=None):
            return db_client.delete(f"/v1/projects/{self.pid}/members/{user}", headers=by or self.admin)

        def count(self, sql, **params):
            with owner_engine.connect() as conn:
                return conn.execute(text(sql), {"t": tenant, "p": self.pid} | params).scalar()

        def audits(self, action):
            return self.count(
                "SELECT count(*) FROM audit_events WHERE tenant_id=:t AND action=:a AND outcome='SUCCESS'",
                a=action,
            )

        def outbox(self):
            return self.count(
                "SELECT count(*) FROM outbox_events WHERE tenant_id=:t AND type='project.member.changed'"
            )

    e = Env()
    e.tenant = tenant
    e.tenant_admin = e.h("alice", ("tenant_admin",))
    ws = db_client.post(
        "/v1/workspaces", json={"slug": "main", "name": "Main"}, headers=e.tenant_admin
    ).json()
    # alice (tenant_admin) creates the project, so she is its first project_admin member.
    project = db_client.post(
        f"/v1/workspaces/{ws['id']}/projects", json={"key": "MEMB", "name": "Members"}, headers=e.tenant_admin
    ).json()
    e.pid = project["id"]
    e.admin = e.h("alice")  # alice as a plain member holding project_admin (no tenant role)
    for user in ("alice", "dave", "paula", "erin"):
        e.signin(user)
    return e


# ---------------------------------------------------------------- directory


def test_signin_registers_user_in_tenant_directory_only(env):
    env.signin("bob", tenant_id=uuid.uuid4())  # another tenant
    r = env.client.get("/v1/users?limit=100", headers=env.h("dave"))
    assert r.status_code == 200
    users = {u["user_id"]: u for u in r.json()["items"]}
    assert set(users) == {"alice", "dave", "paula", "erin"}
    assert users["dave"]["email"] == "dave@example.test"


def test_directory_search_and_pagination(env):
    r = env.client.get("/v1/users?q=PA", headers=env.h("dave")).json()
    assert [u["user_id"] for u in r["items"]] == ["paula"]
    assert env.client.get("/v1/users?q=%25", headers=env.h("dave")).json()["items"] == []  # '%' is literal
    first = env.client.get("/v1/users?limit=3", headers=env.h("dave")).json()
    second = env.client.get(f"/v1/users?limit=3&cursor={first['next_cursor']}", headers=env.h("dave")).json()
    ids = [u["user_id"] for u in first["items"] + second["items"]]
    assert ids == ["alice", "dave", "erin", "paula"] and second["next_cursor"] is None
    assert env.client.get("/v1/users?cursor=bad!", headers=env.h("dave")).status_code == 400


# ---------------------------------------------------------------- add / change / remove


def test_add_change_remove_member_each_audited_once(env):
    r = env.put("dave", "viewer")
    assert r.status_code == 200, r.text
    assert r.json()["roles"] == ["viewer"] and r.json()["email"] == "dave@example.test"
    assert (env.audits("project.member.add"), env.outbox()) == (1, 1)

    again = env.put("dave", "viewer")
    assert again.status_code == 200 and again.json() == r.json()
    assert (env.audits("project.member.add"), env.audits("project.member.update"), env.outbox()) == (1, 0, 1)

    r = env.put("dave", "viewer", "developer")
    assert r.json()["roles"] == ["developer", "viewer"]  # policy order
    assert (env.audits("project.member.update"), env.outbox()) == (1, 2)

    members = env.client.get(f"/v1/projects/{env.pid}/members", headers=env.h("dave")).json()
    assert {m["user_id"]: m["roles"] for m in members} == {
        "alice": ["project_admin"],
        "dave": ["developer", "viewer"],
    }

    assert env.delete("dave").status_code == 204
    assert (env.audits("project.member.remove"), env.outbox()) == (1, 3)
    gone = env.delete("dave")
    assert gone.status_code == 404 and gone.json()["code"] == "MEMBER_NOT_FOUND"
    assert env.outbox() == 3


def test_outbox_event_describes_the_change(env):
    env.put("dave", "tester")
    with_payload = env.count(
        "SELECT payload::text FROM outbox_events WHERE tenant_id=:t AND type='project.member.changed'"
    )
    assert '"new_roles": ["tester"]' in with_payload and '"old_roles": []' in with_payload
    assert '"changed_by": "alice"' in with_payload


def test_removal_is_effective_on_next_request_with_same_token(env):
    env.put("dave", "developer")
    dave = env.h("dave")  # minted once, still valid after removal
    assert env.client.get(f"/v1/projects/{env.pid}", headers=dave).status_code == 200
    assert env.delete("dave").status_code == 204
    r = env.client.get(f"/v1/projects/{env.pid}", headers=dave)
    assert r.status_code == 404 and r.json()["code"] == "NOT_FOUND"
    assert (
        env.count(
            "SELECT count(*) FROM audit_events WHERE tenant_id=:t AND actor_id='dave' AND outcome='DENIED'"
        )
        == 1
    )


def test_downgrade_is_effective_on_next_request_with_same_token(env):
    env.put("dave", "project_admin")
    dave = env.h("dave", **{"If-Match": '"1"'})
    assert (
        env.client.patch(f"/v1/projects/{env.pid}", json={"name": "By dave"}, headers=dave).status_code == 200
    )
    env.put("dave", "viewer")
    dave = env.h("dave", **{"If-Match": '"2"'})
    r = env.client.patch(f"/v1/projects/{env.pid}", json={"name": "Again"}, headers=dave)
    assert r.status_code == 403 and r.json()["required_permission"] == "project.update"
    assert env.client.get(f"/v1/projects/{env.pid}", headers=dave).status_code == 200


def test_non_admin_cannot_manage_members(env):
    env.put("dave", "developer")
    for r in (env.put("erin", "viewer", by=env.h("dave")), env.delete("alice", by=env.h("dave"))):
        assert r.status_code == 403 and r.json()["required_permission"] == "project.member.manage"
    assert env.outbox() == 1


def test_tenant_admin_manages_members_without_membership(env):
    tina = env.h("tina", ("tenant_admin",))  # not a member of the project
    assert env.put("erin", "qa_lead", by=tina).status_code == 200
    assert env.delete("erin", by=tina).status_code == 204


@pytest.mark.parametrize("body", [{"roles": []}, {"roles": ["tenant_admin"]}, {"roles": ["owner"]}, {}])
def test_invalid_role_sets_are_rejected(env, body):
    r = env.client.put(f"/v1/projects/{env.pid}/members/dave", json=body, headers=env.admin)
    assert r.status_code == 422
    assert env.outbox() == 0


# ---------------------------------------------------------------- tenant boundary


def test_unknown_or_foreign_user_cannot_be_added(env, owner_engine):
    other = uuid.uuid4()
    env.signin("mallory", tenant_id=other)  # exists, but in another tenant
    for user in ("mallory", "never-signed-in"):
        r = env.put(user, "viewer")
        assert r.status_code == 404 and r.json()["code"] == "USER_NOT_IN_TENANT", r.text
    assert env.count("SELECT count(*) FROM project_members WHERE tenant_id=:t AND user_id='mallory'") == 0
    assert env.count("SELECT count(*) FROM project_members WHERE user_id='mallory'") == 0
    assert env.outbox() == 0
    denied = env.count(
        "SELECT count(*) FROM audit_events WHERE tenant_id=:t AND outcome='DENIED' "
        "AND reason='user not in tenant directory'"
    )
    assert denied == 2
    with owner_engine.connect() as conn:
        assert (
            conn.execute(text("SELECT count(*) FROM audit_events WHERE tenant_id=:o"), {"o": other}).scalar()
            == 0
        )


# ---------------------------------------------------------------- last admin


def test_last_project_admin_cannot_be_removed_or_downgraded(env):
    for r in (env.delete("alice"), env.put("alice", "viewer")):
        assert r.status_code == 409 and r.json()["code"] == "LAST_PROJECT_ADMIN"
    env.put("paula", "project_admin")
    assert env.put("alice", "viewer").status_code == 200  # another admin exists now
    r = env.delete("paula", by=env.h("paula"))  # paula is now the only admin, even removing herself
    assert r.status_code == 409


def test_concurrent_removal_of_two_admins_leaves_one(env):
    env.put("paula", "project_admin")
    barrier = threading.Barrier(2)
    results: list[int] = []

    def remove(user):
        try:
            barrier.wait(timeout=3)
        except threading.BrokenBarrierError:
            pass
        results.append(env.delete(user, by=env.tenant_admin).status_code)

    threads = [threading.Thread(target=remove, args=(u,)) for u in ("alice", "paula")]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert sorted(results) == [204, 409]
    admins = env.count("SELECT count(*) FROM project_members WHERE project_id=:p AND role='project_admin'")
    assert admins == 1


def test_parallel_burst_never_removes_every_admin(env):
    admins = ["alice", "dave", "paula", "erin"]
    for user in admins[1:]:
        env.put(user, "project_admin")
    barrier = threading.Barrier(len(admins))
    results: list[int] = []

    def remove(user):
        try:
            barrier.wait(timeout=3)
        except threading.BrokenBarrierError:
            pass
        results.append(env.delete(user, by=env.tenant_admin).status_code)

    threads = [threading.Thread(target=remove, args=(u,)) for u in admins]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert sorted(results) == [204, 204, 204, 409]
    assert env.count("SELECT count(*) FROM project_members WHERE project_id=:p AND role='project_admin'") == 1


# ---------------------------------------------------------------- effective permissions


@pytest.mark.parametrize("role", policy.PROJECT_ROLES)
def test_permissions_me_matches_policy(env, role):
    assert env.put("dave", role).status_code == 200
    r = env.client.get(f"/v1/projects/{env.pid}/permissions/me", headers=env.h("dave"))
    assert r.status_code == 200
    body = r.json()
    mine = frozenset({role})
    assert body["member_roles"] == [role] and body["roles"] == [role]
    assert set(body["permissions"]) == policy.project_permissions(frozenset(), mine)
    assert set(body["approval_scopes"]) == policy.approval_scopes(frozenset(), mine)
    assert body["tenant_roles"] == []


def test_permissions_me_for_tenant_admin_and_auditor(env):
    tina = env.h("tina", ("tenant_admin",))  # tenant admin who is not a member
    admin = env.client.get(f"/v1/projects/{env.pid}/permissions/me", headers=tina).json()
    assert admin["member_roles"] == [] and admin["roles"] == ["project_admin"]
    assert set(admin["permissions"]) == set(policy.PROJECT_PERMISSIONS)
    auditor = env.client.get(
        f"/v1/projects/{env.pid}/permissions/me", headers=env.h("audrey", ("auditor",))
    ).json()
    assert auditor["permissions"] == ["audit.read", "project.read"] and auditor["approval_scopes"] == []


def test_permissions_me_for_non_member_is_masked(env):
    r = env.client.get(f"/v1/projects/{env.pid}/permissions/me", headers=env.h("erin"))
    assert r.status_code == 404
