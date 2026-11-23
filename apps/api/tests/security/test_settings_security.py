"""INPROOM-104 / story INPROOM-21 AC 2, 3, 8: production-enable authority, concurrency and retries for
settings and lifecycle, against the live API. Route x role coverage of the new routes lives in
test_rbac_matrix.py (ROUTE_RULES) and cross-tenant coverage in test_tenant_isolation.py (ROUTES)."""

import threading
import uuid

import pytest
from sqlalchemy import text

from app.authz import policy
from tests.auth_helpers import bearer
from tests.security.conftest import record_rbac

pytestmark = pytest.mark.integration

PROD = {
    "production": {"actions_enabled": True, "environments": ["prod"]},
    "approvals": {"required_scopes": ["release"]},
}


@pytest.fixture
def world(db_client, tokens, owner_engine):
    tenant = uuid.uuid4()

    class World:
        client = db_client

        def h(self, user, roles=(), tenant_id=None, **extra):
            return bearer(tokens.mint(tenant_id=tenant_id or tenant, user_id=user, roles=list(roles))) | extra

        def member(self, user, role):
            db_client.get("/v1/me", headers=self.h(user))
            r = db_client.put(
                f"/v1/projects/{self.pid}/members/{user}", json={"roles": [role]}, headers=self.owner
            )
            assert r.status_code == 200, r.text

        def q(self, sql, **params):
            with owner_engine.connect() as conn:
                return conn.execute(text(sql), {"t": tenant, "p": self.pid} | params).scalar()

        def settings_versions(self):
            return self.q("SELECT count(*) FROM project_settings_versions WHERE project_id=:p")

    w = World()
    w.tenant = tenant
    w.owner = w.h("owner", ("tenant_admin",))
    ws = db_client.post("/v1/workspaces", json={"slug": "sec", "name": "Sec"}, headers=w.owner).json()
    w.pid = db_client.post(
        f"/v1/workspaces/{ws['id']}/projects", json={"key": "SEC", "name": "Secret settings"}, headers=w.owner
    ).json()["id"]
    return w


# actor -> (token roles, membership role, same tenant, expected status, required permission on 403)
PROD_ACTORS = {
    **{
        f"member:{r}": (
            (),
            r,
            True,
            "403",
            "project.production.enable" if r == policy.PROJECT_ADMIN else "project.settings.update",
        )
        for r in policy.PROJECT_ROLES
    },
    "auditor + member:project_admin": (("auditor",), "project_admin", True, "403", "project.settings.update"),
    "tenant_admin (not a member)": (("tenant_admin",), None, True, "200", None),
    "other tenant's tenant_admin": (("tenant_admin",), None, False, "404", None),
}


@pytest.mark.parametrize("actor", list(PROD_ACTORS))
def test_only_a_tenant_admin_can_enable_production(world, actor):
    roles, member, same, expected, required = PROD_ACTORS[actor]
    user = f"u-{uuid.uuid4().hex[:6]}"
    if member:
        world.member(user, member)
    tenant = world.tenant if same else uuid.uuid4()
    headers = world.h(user, roles, tenant_id=tenant, **{"If-Match": '"1"'})
    r = world.client.put(f"/v1/projects/{world.pid}/settings", json={"settings": PROD}, headers=headers)
    record_rbac(
        "PUT /v1/projects/{project_id}/settings",
        f"enable production as {actor}",
        expected,
        str(r.status_code),
    )
    assert str(r.status_code) == expected, r.text
    if expected == "200":
        assert r.json()["settings"]["production"]["enabled_by"] == user
        assert world.settings_versions() == 2
        return
    assert world.settings_versions() == 1
    assert "Secret settings" not in r.text
    if required:
        assert r.json()["required_permission"] == required
    denied = world.q(
        "SELECT count(*) FROM audit_events WHERE tenant_id=:tt AND actor_id=:a AND outcome='DENIED'",
        tt=tenant,
        a=user,
    )
    assert denied == 1


def _parallel(n, fn):
    barrier = threading.Barrier(n)
    results: list = [None] * n

    def run(i):
        try:
            barrier.wait(timeout=3)
        except threading.BrokenBarrierError:
            pass
        results[i] = fn(i)

    threads = [threading.Thread(target=run, args=(i,)) for i in range(n)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    return results


def test_concurrent_saves_from_one_version_exactly_one_wins(world):
    modes = ["draft_only", "approved_writes", "read_only", "draft_only", "approved_writes", "draft_only"]

    def save(i):
        body = {
            "settings": {
                "write_mode": modes[i],
                "approvals": {"required_scopes": ["plan"], "expiry_hours": 10 + i},
            }
        }
        return world.client.put(
            f"/v1/projects/{world.pid}/settings", json=body, headers=world.owner | {"If-Match": '"1"'}
        ).status_code

    statuses = _parallel(len(modes), save)
    assert sorted(statuses) == [200] + [412] * (len(modes) - 1), statuses
    assert world.settings_versions() == 2
    assert (
        world.q(
            "SELECT count(*) FROM audit_events WHERE tenant_id=:t "
            "AND action='project.settings.update' AND outcome='SUCCESS'"
        )
        == 1
    )
    assert (
        world.q("SELECT count(*) FROM outbox_events WHERE tenant_id=:t AND type='project.settings.changed'")
        == 1
    )
    record_rbac(
        "PUT /v1/projects/{project_id}/settings", "6 concurrent saves, same If-Match", "1 wins", "1 wins"
    )


def test_concurrent_identical_retries_create_one_version(world):
    body = {"settings": {"write_mode": "draft_only"}}

    def save(i):
        return world.client.put(
            f"/v1/projects/{world.pid}/settings", json=body, headers=world.owner | {"If-Match": '"1"'}
        ).status_code

    statuses = _parallel(5, save)
    assert sorted(statuses) == [200, 412, 412, 412, 412], statuses
    assert world.settings_versions() == 2


@pytest.mark.parametrize("action", ["archive", "restore"])
def test_concurrent_lifecycle_actions_transition_once(world, action):
    if action == "restore":
        assert world.client.post(f"/v1/projects/{world.pid}/archive", headers=world.owner).status_code == 200
    sql = (
        "SELECT count(*) FROM audit_events WHERE tenant_id=:t "
        f"AND action='project.{action}' AND outcome='SUCCESS'"
    )
    before = world.q(sql)

    def act(i):
        return world.client.post(
            f"/v1/projects/{world.pid}/lifecycle", json={"action": action}, headers=world.owner
        ).status_code

    statuses = _parallel(6, act)
    final = "ARCHIVED" if action == "archive" else "ACTIVE"
    if action == "archive":
        assert statuses == [200] * 6  # one transition, five no-ops
    else:
        assert sorted(statuses) == [200] + [409] * 5  # once ACTIVE, restore is no longer allowed
    assert world.client.get(f"/v1/projects/{world.pid}", headers=world.owner).json()["status"] == final
    after = world.q(sql)
    assert after - before == 1


def test_other_tenant_cannot_read_settings_history_or_change_status(world):
    attacker = world.h("eve", ("tenant_admin",), tenant_id=uuid.uuid4())
    for method, path, body in (
        ("GET", "/settings", None),
        ("GET", "/settings/versions", None),
        ("GET", "/settings/versions/1", None),
        ("POST", "/lifecycle", {"action": "archive"}),
    ):
        r = world.client.request(method, f"/v1/projects/{world.pid}{path}", json=body, headers=attacker)
        assert r.status_code == 404 and "Secret settings" not in r.text, path
    assert world.client.get(f"/v1/projects/{world.pid}", headers=world.owner).json()["status"] == "ACTIVE"
