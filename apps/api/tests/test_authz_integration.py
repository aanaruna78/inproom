"""INPROOM-93 (story INPROOM-20 AC 1, 4, 6, 8): authorize() on the real API and database."""

import uuid

import pytest
from fastapi import Request
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

from app.auth import CurrentPrincipal
from app.authz import authorize
from app.db import tenant_session
from app.main import create_app
from tests.auth_helpers import bearer

pytestmark = pytest.mark.integration


def build_app():
    """The real app plus a test-only approval decision route; E06 approval endpoints call authorize() the
    same way."""
    app = create_app()

    @app.post("/test/projects/{project_id}/approvals/{scope}")
    def decide(
        project_id: uuid.UUID, scope: str, proposer: str, request: Request, principal: CurrentPrincipal
    ):
        with tenant_session(principal.tenant_id) as conn:
            access = authorize(
                conn, principal, request, "approval.decide", project_id, scope=scope, proposer_id=proposer
            )
        return {"decided_by": principal.user_id, "roles": sorted(access.roles)}

    return app


@pytest.fixture
def env(client_for, test_database_url, tokens, owner_engine):
    client = client_for(test_database_url, build_app())
    tenant = uuid.uuid4()

    class Env:
        def h(self, user="alice", roles=("tenant_admin",), tenant_id=None):
            return bearer(tokens.mint(tenant_id=tenant_id or tenant, user_id=user, roles=list(roles)))

        def member(self, project_id, user, *roles):
            with owner_engine.begin() as conn:
                for role in roles:
                    conn.execute(
                        text(
                            "INSERT INTO project_members (tenant_id, project_id, user_id, role, added_by) "
                            "VALUES (:t, :p, :u, :r, 'test')"
                        ),
                        {"t": tenant, "p": project_id, "u": user, "r": role},
                    )

        def audits(self, actor, outcome="DENIED"):
            with owner_engine.connect() as conn:
                return conn.execute(
                    text(
                        "SELECT action, target_id, project_id, reason FROM audit_events "
                        "WHERE tenant_id = :t AND actor_id = :a AND outcome = :o ORDER BY occurred_at"
                    ),
                    {"t": tenant, "a": actor, "o": outcome},
                ).all()

    e = Env()
    e.client, e.tenant = client, tenant
    ws = client.post("/v1/workspaces", json={"slug": "main", "name": "Main"}, headers=e.h()).json()
    e.project = client.post(
        f"/v1/workspaces/{ws['id']}/projects", json={"key": "RBAC", "name": "Rbac project"}, headers=e.h()
    ).json()
    e.workspace = ws
    return e


def test_token_project_role_without_membership_gets_masked_404(env):
    pid = env.project["id"]
    h = env.h("mallory", ("developer", "project_admin"))  # project roles in the token are ignored
    assert env.client.get(f"/v1/projects/{pid}", headers=h).status_code == 404
    listed = env.client.get(f"/v1/workspaces/{env.workspace['id']}/projects", headers=h).json()["items"]
    assert listed == []
    assert env.audits("mallory") == [("project.read", pid, None, "resource not found or not accessible")]


def test_member_without_permission_gets_403_with_required_permission(env):
    pid = env.project["id"]
    env.member(pid, "dave", "developer")
    h = env.h("dave", ()) | {"If-Match": '"1"'}
    assert env.client.get(f"/v1/projects/{pid}", headers=h).status_code == 200
    r = env.client.patch(f"/v1/projects/{pid}", json={"name": "Hijacked"}, headers=h)
    assert r.status_code == 403
    body = r.json()
    assert body["code"] == "FORBIDDEN" and body["required_permission"] == "project.update"
    assert r.headers["content-type"].startswith("application/problem+json")
    [(action, target, project_id, reason)] = env.audits("dave")
    assert (action, target, str(project_id), reason) == (
        "project.update",
        pid,
        pid,
        "missing permission project.update",
    )
    assert env.client.post(f"/v1/projects/{pid}/archive", headers=h).status_code == 403


def test_project_admin_member_without_tenant_role_can_administer(env):
    pid = env.project["id"]
    env.member(pid, "paula", "project_admin")
    h = env.h("paula", ()) | {"If-Match": '"1"'}
    r = env.client.patch(f"/v1/projects/{pid}", json={"name": "Renamed by PA"}, headers=h)
    assert r.status_code == 200, r.text
    assert r.json()["my_roles"] == ["project_admin"]


def test_auditor_reads_every_project_but_cannot_write(env):
    pid = env.project["id"]
    h = env.h("audrey", ("auditor",))
    assert env.client.get(f"/v1/projects/{pid}", headers=h).status_code == 200
    listed = env.client.get(f"/v1/workspaces/{env.workspace['id']}/projects", headers=h).json()["items"]
    assert [p["id"] for p in listed] == [pid]
    r = env.client.patch(f"/v1/projects/{pid}", json={"name": "Nope"}, headers=h | {"If-Match": '"1"'})
    assert r.status_code == 403
    r = env.client.post("/v1/workspaces", json={"slug": "x-ws", "name": "x"}, headers=h)
    assert r.status_code == 403 and r.json()["required_permission"] == "tenant.workspace.create"


@pytest.mark.parametrize(
    ("role", "scope", "expected"),
    [
        ("tech_lead", "plan", 200),
        ("tech_lead", "impact", 200),
        ("tech_lead", "release", 403),
        ("product_owner", "requirement", 200),
        ("product_owner", "test", 403),
        ("qa_lead", "test", 200),
        ("release_manager", "release", 200),
        ("developer", "plan", 403),
        ("viewer", "requirement", 403),
        ("project_admin", "release", 200),
    ],
)
def test_approver_scopes_enforced_server_side(env, role, scope, expected):
    pid = env.project["id"]
    env.member(pid, "ann", role)
    r = env.client.post(
        f"/test/projects/{pid}/approvals/{scope}?proposer=someone-else", headers=env.h("ann", ())
    )
    assert r.status_code == expected, r.text
    if expected == 403:
        assert r.json()["code"] == "FORBIDDEN" and r.json()["required_permission"] == "approval.decide"
        assert len(env.audits("ann")) == 1


@pytest.mark.parametrize(
    ("user", "roles", "member_role"), [("ann", (), "tech_lead"), ("alice", ("tenant_admin",), None)]
)
def test_self_approval_forbidden_even_for_admins(env, user, roles, member_role):
    pid = env.project["id"]
    if member_role:
        env.member(pid, user, member_role)
    r = env.client.post(f"/test/projects/{pid}/approvals/plan?proposer={user}", headers=env.h(user, roles))
    assert r.status_code == 403
    assert r.json()["code"] == "SELF_APPROVAL_FORBIDDEN"
    [(action, _, _, reason)] = env.audits(user)
    assert action == "approval.decide" and "own proposal" in reason


def test_approval_on_foreign_project_is_masked(env, tokens):
    pid = env.project["id"]
    other = env.h("eve", ("tenant_admin",), tenant_id=uuid.uuid4())
    r = env.client.post(f"/test/projects/{pid}/approvals/plan?proposer=x", headers=other)
    assert r.status_code == 404


def test_database_rejects_roles_outside_the_project_set(env):
    with pytest.raises(IntegrityError, match="ck_project_members_role"):
        env.member(env.project["id"], "zed", "tenant_admin")
