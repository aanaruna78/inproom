"""INPROOM-95 / story INPROOM-20 AC 1, 2, 3, 4, 8: every route x every role against the live API.

Expected outcomes are written independently of app.authz.policy: project permissions come from the agreed
matrix in tests/test_authz_policy.py (EXPECTED) and the visibility/tenant rules below. A wrong policy, a
skipped membership check or a route asking for the wrong permission all fail this suite.

Every API route must appear in ROUTE_RULES, or test_every_route_has_an_rbac_rule fails - a new endpoint
cannot ship without RBAC cases.
"""

import uuid
from dataclasses import dataclass

import pytest
from sqlalchemy import text

from app.authz import policy
from tests.auth_helpers import bearer
from tests.fakes.github import OTHER_TOKEN, VALID_TOKEN
from tests.security.conftest import record_rbac
from tests.security.test_tenant_isolation import app_routes
from tests.test_authz_policy import EXPECTED

# Agreed tenant-level rules (independent of policy.py).
TENANT_EXPECTED = {
    "tenant.read": {"*"},  # every signed-in user of the tenant
    "tenant.workspace.create": {"tenant_admin"},
    "workspace.project.create": {"tenant_admin"},
    "tenant.connection.manage": {"tenant_admin"},
}
AUDITOR_CAP = {"project.read", "audit.read"}
# Agreed project rules for tenant roles without membership.
TENANT_ROLE_IN_PROJECT = {
    "tenant_admin": set(EXPECTED),  # acts as project_admin
    "auditor": {"project.read", "audit.read"},
}

pytestmark = pytest.mark.integration

# route -> (level, permission). level: public | self | tenant | workspace | project
ROUTE_RULES: dict[tuple[str, str], tuple[str, str | None]] = {
    ("GET", "/healthz"): ("public", None),
    ("GET", "/readyz"): ("public", None),
    ("GET", "/v1/me"): ("self", None),
    ("POST", "/v1/workspaces"): ("tenant", "tenant.workspace.create"),
    ("GET", "/v1/workspaces"): ("tenant", "tenant.read"),
    ("GET", "/v1/users"): ("tenant", "tenant.read"),
    ("POST", "/v1/workspaces/{workspace_id}/projects"): ("workspace", "workspace.project.create"),
    ("GET", "/v1/workspaces/{workspace_id}/projects"): ("workspace", "tenant.read"),
    ("GET", "/v1/projects/{project_id}"): ("project", "project.read"),
    ("PATCH", "/v1/projects/{project_id}"): ("project", "project.update"),
    ("POST", "/v1/projects/{project_id}/archive"): ("project", "project.archive"),
    ("GET", "/v1/projects/{project_id}/members"): ("project", "project.read"),
    ("PUT", "/v1/projects/{project_id}/members/{user_id}"): ("project", "project.member.manage"),
    ("DELETE", "/v1/projects/{project_id}/members/{user_id}"): ("project", "project.member.manage"),
    ("GET", "/v1/projects/{project_id}/permissions/me"): ("project", "project.read"),
    ("GET", "/v1/projects/{project_id}/settings"): ("project", "project.read"),
    ("PUT", "/v1/projects/{project_id}/settings"): ("project", "project.settings.update"),
    ("GET", "/v1/projects/{project_id}/settings/versions"): ("project", "project.read"),
    ("GET", "/v1/projects/{project_id}/settings/versions/{version}"): ("project", "project.read"),
    ("POST", "/v1/projects/{project_id}/lifecycle"): ("project", "project.archive"),
    # INPROOM-109
    ("POST", "/v1/connections"): ("tenant", "tenant.connection.manage"),
    ("GET", "/v1/connections"): ("tenant", "tenant.read"),
    ("GET", "/v1/connections/{connection_id}"): ("connection", "tenant.read"),
    ("POST", "/v1/connections/{connection_id}/check"): ("connection", "tenant.connection.manage"),
    ("PUT", "/v1/connections/{connection_id}/credential"): ("connection", "tenant.connection.manage"),
    ("POST", "/v1/connections/{connection_id}/revoke"): ("connection", "tenant.connection.manage"),
    ("GET", "/v1/connections/{connection_id}/repositories"): ("project", "project.source.manage"),
    ("GET", "/v1/projects/{project_id}/sources/repositories"): ("project", "project.read"),
    ("PUT", "/v1/projects/{project_id}/sources/repositories/{connection_id}/{repo_id}"): (
        "project",
        "project.source.manage",
    ),
    ("DELETE", "/v1/projects/{project_id}/sources/repositories/{connection_id}/{repo_id}"): (
        "project",
        "project.source.manage",
    ),
    ("GET", "/v1/projects/{project_id}/sources/scope"): ("project", "project.read"),
}
# Audit action recorded for a denial when it differs from the permission (listing projects of a workspace
# the caller cannot see is audited as a project read).
AUDIT_ACTION = {("GET", "/v1/workspaces/{workspace_id}/projects"): "project.read"}
TESTED = [r for r, (level, _) in ROUTE_RULES.items() if level not in ("public", "self")]
SUCCESS = {"GET": "200", "PATCH": "200", "PUT": "200", "DELETE": "204", "POST": "200"}

# actor -> (token roles, project membership role or None, same tenant?)
ACTORS: dict[str, tuple[tuple[str, ...], str | None, bool]] = {
    **{f"member:{r}": ((), r, True) for r in policy.PROJECT_ROLES},
    "tenant_admin (not a member)": (("tenant_admin",), None, True),
    "auditor (not a member)": (("auditor",), None, True),
    "tenant member, not in project": ((), None, True),
    "token claims developer, not in project": (("developer", "project_admin"), None, True),
    "other tenant's tenant_admin": (("tenant_admin",), None, False),
    # INPROOM-99: an auditor who is also a project member stays read-only, whatever the project role.
    **{f"auditor + member:{r}": (("auditor",), r, True) for r in policy.PROJECT_ROLES},
}


def test_every_route_has_an_rbac_rule():
    missing = app_routes() - ROUTE_RULES.keys()
    stale = ROUTE_RULES.keys() - app_routes()
    assert not missing, f"Add RBAC rules and cases for: {sorted(missing)}"
    assert not stale, f"ROUTE_RULES lists routes that no longer exist: {sorted(stale)}"


@dataclass
class World:
    tenant: uuid.UUID
    workspace: dict
    project: dict
    owner: dict
    connection: dict


@pytest.fixture
def world(db_client, tokens, fake_github):
    tenant = uuid.uuid4()
    owner = bearer(tokens.mint(tenant_id=tenant, user_id="owner", roles=["tenant_admin"]))
    ws = db_client.post("/v1/workspaces", json={"slug": "main", "name": "Main"}, headers=owner).json()
    project = db_client.post(
        f"/v1/workspaces/{ws['id']}/projects", json={"key": "SECRET", "name": "Secret project"}, headers=owner
    ).json()
    # A second member ("target") that member-management routes act on.
    target = bearer(tokens.mint(tenant_id=tenant, user_id="target", roles=[]))
    assert db_client.get("/v1/me", headers=target).status_code == 200
    r = db_client.put(
        f"/v1/projects/{project['id']}/members/target", json={"roles": ["viewer"]}, headers=owner
    )
    assert r.status_code == 200, r.text
    # A Git connection with acme/api (repo 1) selected: DELETE acts on it, PUT selects acme/web (repo 2).
    connection = db_client.post(
        "/v1/connections", json={"display_name": "GitHub", "token": VALID_TOKEN}, headers=owner
    ).json()
    r = db_client.put(
        f"/v1/projects/{project['id']}/sources/repositories/{connection['id']}/1", json={}, headers=owner
    )
    assert r.status_code == 200, r.text
    return World(tenant, ws, project, owner, connection)


def _tenant_allows(roles: tuple[str, ...], permission: str) -> bool:
    allowed = TENANT_EXPECTED[permission]
    return "*" in allowed or bool(allowed & set(roles))


def _project_permissions(roles: tuple[str, ...], member: str | None) -> set[str] | None:
    """None = project not visible to this actor."""
    granted: set[str] = set()
    visible = False
    if member:
        visible = True
        granted |= {perm for perm, holders in EXPECTED.items() if member in holders}
    for role in roles:
        if role in TENANT_ROLE_IN_PROJECT:  # project role names in the token grant nothing
            visible = True
            granted |= TENANT_ROLE_IN_PROJECT[role]
    if "auditor" in roles and "tenant_admin" not in roles:
        granted &= AUDITOR_CAP  # agreed rule: auditors are read-only everywhere
    return granted if visible else None


def _expected(
    method: str, level: str, permission: str, roles: tuple[str, ...], member: str | None, same: bool
):
    ok = "201" if (method, level) in {("POST", "tenant"), ("POST", "workspace")} else SUCCESS[method]
    if level == "tenant":
        return ok if _tenant_allows(roles, permission) else "403"
    if level in ("workspace", "connection"):
        if not _tenant_allows(roles, permission):
            return "403"
        return ok if same else "404"
    granted = _project_permissions(roles, member) if same else None
    if granted is None:
        return "404"
    return ok if permission in granted else "403"


def _call(client, method, path, world: World, headers):
    url = path.format(
        workspace_id=world.workspace["id"],
        project_id=world.project["id"],
        user_id="target",
        version=1,
        connection_id=world.connection["id"],
        repo_id="2" if method == "PUT" else "1",
    )
    if path == "/v1/connections/{connection_id}/repositories":
        url += f"?project_id={world.project['id']}"
    kwargs: dict = {"headers": dict(headers)}
    if method == "POST" and path == "/v1/connections":
        kwargs["json"] = {"display_name": f"c-{uuid.uuid4().hex[:8]}", "token": VALID_TOKEN}
    elif method == "PUT" and path.endswith("/credential"):
        kwargs["json"] = {"token": OTHER_TOKEN}
    elif method == "PUT" and "/sources/repositories/" in path:
        kwargs["json"] = {"branches": []}
    elif method == "POST" and path == "/v1/workspaces":
        kwargs["json"] = {"slug": f"ws-{uuid.uuid4().hex[:8]}", "name": "New"}
    elif method == "POST" and path.endswith("/lifecycle"):
        kwargs["json"] = {"action": "archive", "reason": "matrix"}
    elif method == "POST" and path.endswith("/projects"):
        kwargs["json"] = {"key": "N" + uuid.uuid4().hex[:6].upper(), "name": "New project"}
    elif method == "PATCH":
        kwargs["json"] = {"name": "Renamed"}
        kwargs["headers"]["If-Match"] = f'"{world.project["version"]}"'
    elif method == "PUT" and path.endswith("/settings"):
        kwargs["json"] = {"settings": {"write_mode": "draft_only"}, "change_note": "matrix"}
        kwargs["headers"]["If-Match"] = '"1"'
    elif method == "PUT":
        kwargs["json"] = {"roles": ["tester"]}
    return client.request(method, url, **kwargs)


def _denials(owner_engine, tenant, actor_id):
    with owner_engine.connect() as conn:
        return conn.execute(
            text(
                "SELECT action, project_id FROM audit_events "
                "WHERE tenant_id=:t AND actor_id=:a AND outcome='DENIED'"
            ),
            {"t": tenant, "a": actor_id},
        ).all()


@pytest.mark.parametrize("actor", list(ACTORS))
@pytest.mark.parametrize(("method", "path"), TESTED, ids=[f"{m} {p}" for m, p in TESTED])
def test_route_x_role(db_client, tokens, world, owner_engine, method, path, actor):
    roles, member, same = ACTORS[actor]
    level, permission = ROUTE_RULES[(method, path)]
    tenant = world.tenant if same else uuid.uuid4()
    actor_id = f"actor-{uuid.uuid4().hex[:8]}"
    headers = bearer(tokens.mint(tenant_id=tenant, user_id=actor_id, roles=list(roles)))
    if member:
        assert db_client.get("/v1/me", headers=headers).status_code == 200
        r = db_client.put(
            f"/v1/projects/{world.project['id']}/members/{actor_id}",
            json={"roles": [member]},
            headers=world.owner,
        )
        assert r.status_code == 200, r.text

    expected = _expected(method, level, permission, roles, member, same)
    r = _call(db_client, method, path, world, headers)
    record_rbac(f"{method} {path}", actor, expected, str(r.status_code))
    assert str(r.status_code) == expected, (actor, r.text)

    denials = _denials(owner_engine, tenant, actor_id)
    if expected in ("403", "404"):
        body = r.json()
        assert body["code"] == ("FORBIDDEN" if expected == "403" else "NOT_FOUND")
        if expected == "403":
            assert body["required_permission"] == permission
        # Exactly one DENIED audit in the caller's own tenant, naming the permission that was checked.
        assert [a for a, _ in denials] == [AUDIT_ACTION.get((method, path), permission)], denials
        assert "Secret project" not in r.text and "SECRET" not in r.text
    else:
        assert denials == []


# ---------------------------------------------------------------- revocation (AC 2, 3)

PROJECT_ROUTES = [r for r in TESTED if ROUTE_RULES[r][0] == "project"]


@pytest.mark.parametrize(("method", "path"), PROJECT_ROUTES, ids=[f"{m} {p}" for m, p in PROJECT_ROUTES])
def test_removed_member_loses_every_route_with_unexpired_token(db_client, tokens, world, method, path):
    headers = bearer(tokens.mint(tenant_id=world.tenant, user_id="leaver", roles=[]))
    db_client.get("/v1/me", headers=headers)
    url = f"/v1/projects/{world.project['id']}/members/leaver"
    assert db_client.put(url, json={"roles": ["project_admin"]}, headers=world.owner).status_code == 200
    assert db_client.get(f"/v1/projects/{world.project['id']}", headers=headers).status_code == 200
    assert db_client.delete(url, headers=world.owner).status_code == 204
    r = _call(db_client, method, path, world, headers)  # same token, still unexpired
    record_rbac(f"{method} {path}", "removed member, same token", "404", str(r.status_code))
    assert r.status_code == 404


ADMIN_ROUTES = [
    r for r in PROJECT_ROUTES if policy.PROJECT_PERMISSIONS[ROUTE_RULES[r][1]] == policy.Kind.ADMIN
]


@pytest.mark.parametrize(("method", "path"), ADMIN_ROUTES, ids=[f"{m} {p}" for m, p in ADMIN_ROUTES])
def test_downgraded_admin_loses_admin_routes_with_unexpired_token(db_client, tokens, world, method, path):
    headers = bearer(tokens.mint(tenant_id=world.tenant, user_id="demoted", roles=[]))
    db_client.get("/v1/me", headers=headers)
    url = f"/v1/projects/{world.project['id']}/members/demoted"
    db_client.put(url, json={"roles": ["project_admin"]}, headers=world.owner)
    db_client.put(url, json={"roles": ["viewer"]}, headers=world.owner)
    r = _call(db_client, method, path, world, headers)
    record_rbac(
        f"{method} {path}", "project_admin downgraded to viewer, same token", "403", str(r.status_code)
    )
    assert r.status_code == 403
    assert db_client.get(f"/v1/projects/{world.project['id']}", headers=headers).status_code == 200


def test_re_added_member_regains_access(db_client, tokens, world):
    headers = bearer(tokens.mint(tenant_id=world.tenant, user_id="returner", roles=[]))
    db_client.get("/v1/me", headers=headers)
    url = f"/v1/projects/{world.project['id']}/members/returner"
    project = f"/v1/projects/{world.project['id']}"
    db_client.put(url, json={"roles": ["developer"]}, headers=world.owner)
    db_client.delete(url, headers=world.owner)
    assert db_client.get(project, headers=headers).status_code == 404
    db_client.put(url, json={"roles": ["developer"]}, headers=world.owner)
    assert db_client.get(project, headers=headers).status_code == 200
    record_rbac(
        f"GET {project.replace(world.project['id'], '{project_id}')}", "re-added member", "200", "200"
    )


# ---------------------------------------------------------------- cross-tenant membership attacks (AC 5, 8)


def test_cannot_grant_a_foreign_user_access(db_client, tokens, world, owner_engine):
    other = uuid.uuid4()
    foreigner = bearer(tokens.mint(tenant_id=other, user_id="foreigner", roles=["tenant_admin"]))
    db_client.get("/v1/me", headers=foreigner)  # foreigner exists in tenant B's directory
    url = f"/v1/projects/{world.project['id']}/members/foreigner"
    # 1) tenant A admin tries to add tenant B's user
    r = db_client.put(url, json={"roles": ["project_admin"]}, headers=world.owner)
    record_rbac(
        "PUT /v1/projects/{project_id}/members/{user_id}",
        "add other tenant's user",
        "404",
        str(r.status_code),
    )
    assert r.status_code == 404 and r.json()["code"] == "USER_NOT_IN_TENANT"
    # 2) tenant B admin tries to add themselves to tenant A's project
    r = db_client.put(url, json={"roles": ["project_admin"]}, headers=foreigner)
    record_rbac(
        "PUT /v1/projects/{project_id}/members/{user_id}",
        "other tenant adds itself",
        "404",
        str(r.status_code),
    )
    assert r.status_code == 404 and r.json()["code"] == "NOT_FOUND"
    # 3) the foreigner still sees nothing
    assert db_client.get(f"/v1/projects/{world.project['id']}", headers=foreigner).status_code == 404
    with owner_engine.connect() as conn:
        rows = conn.execute(text("SELECT count(*) FROM project_members WHERE user_id='foreigner'")).scalar()
    assert rows == 0


def test_other_tenant_cannot_remove_or_read_members(db_client, tokens, world, owner_engine):
    attacker = bearer(tokens.mint(tenant_id=uuid.uuid4(), user_id="attacker", roles=["tenant_admin"]))
    base = f"/v1/projects/{world.project['id']}/members"
    assert db_client.get(base, headers=attacker).status_code == 404
    assert db_client.delete(f"{base}/owner", headers=attacker).status_code == 404
    with owner_engine.connect() as conn:
        members = (
            conn.execute(
                text("SELECT user_id FROM project_members WHERE project_id=:p ORDER BY user_id"),
                {"p": world.project["id"]},
            )
            .scalars()
            .all()
        )
    assert members == ["owner", "target"]


def test_directory_never_lists_other_tenants_users(db_client, tokens, world):
    stranger = bearer(tokens.mint(tenant_id=uuid.uuid4(), user_id="stranger", roles=[]))
    db_client.get("/v1/me", headers=stranger)
    users = db_client.get("/v1/users?limit=100", headers=stranger).json()["items"]
    assert [u["user_id"] for u in users] == ["stranger"]
    record_rbac("GET /v1/users", "other tenant lists directory", "own users only", "own users only")
