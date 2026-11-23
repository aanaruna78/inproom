"""INPROOM-85 / story INPROOM-18 AC 4: no cross-tenant read, write or existence leak through any route or
at the database layer.

Every API route must be classified in ROUTES. Adding an endpoint without classifying it fails
`test_every_route_is_classified`, so new endpoints cannot skip isolation testing.
"""

import base64
import json
import uuid
from dataclasses import dataclass

import pytest
from fastapi.routing import APIRoute
from sqlalchemy import text

from app.db import tenant_session
from app.db_schema import TENANT_SCOPED_TABLES
from app.main import create_app
from tests.auth_helpers import bearer
from tests.fakes.github import VALID_TOKEN
from tests.security.conftest import record

pytestmark = pytest.mark.integration

# kind: public | self | tenant_collection | workspace_scoped | project_scoped
ROUTES: dict[tuple[str, str], str] = {
    ("GET", "/healthz"): "public",
    ("GET", "/readyz"): "public",
    ("GET", "/v1/me"): "self",
    ("POST", "/v1/workspaces"): "tenant_collection",
    ("GET", "/v1/workspaces"): "tenant_collection",
    ("POST", "/v1/workspaces/{workspace_id}/projects"): "workspace_scoped",
    ("GET", "/v1/workspaces/{workspace_id}/projects"): "workspace_scoped",
    ("GET", "/v1/projects/{project_id}"): "project_scoped",
    ("PATCH", "/v1/projects/{project_id}"): "project_scoped",
    ("POST", "/v1/projects/{project_id}/archive"): "project_scoped",
    # INPROOM-94: membership, effective permissions, tenant user directory
    ("GET", "/v1/users"): "tenant_collection",
    ("GET", "/v1/projects/{project_id}/members"): "project_scoped",
    ("PUT", "/v1/projects/{project_id}/members/{user_id}"): "project_scoped",
    ("DELETE", "/v1/projects/{project_id}/members/{user_id}"): "project_scoped",
    ("GET", "/v1/projects/{project_id}/permissions/me"): "project_scoped",
    # INPROOM-102: versioned settings
    ("GET", "/v1/projects/{project_id}/settings"): "project_scoped",
    ("PUT", "/v1/projects/{project_id}/settings"): "project_scoped",
    ("GET", "/v1/projects/{project_id}/settings/versions"): "project_scoped",
    ("GET", "/v1/projects/{project_id}/settings/versions/{version}"): "project_scoped",
    # INPROOM-103: lifecycle
    ("POST", "/v1/projects/{project_id}/lifecycle"): "project_scoped",
    # INPROOM-109: Git connections (tenant-level) and project source selection
    ("POST", "/v1/connections"): "tenant_collection",
    ("GET", "/v1/connections"): "tenant_collection",
    ("GET", "/v1/connections/{connection_id}"): "connection_scoped",
    ("POST", "/v1/connections/{connection_id}/check"): "connection_scoped",
    ("PUT", "/v1/connections/{connection_id}/credential"): "connection_scoped",
    ("POST", "/v1/connections/{connection_id}/revoke"): "connection_scoped",
    ("GET", "/v1/connections/{connection_id}/repositories"): "connection_scoped",
    ("GET", "/v1/projects/{project_id}/sources/repositories"): "project_scoped",
    ("PUT", "/v1/projects/{project_id}/sources/repositories/{connection_id}/{repo_id}"): "project_scoped",
    ("DELETE", "/v1/projects/{project_id}/sources/repositories/{connection_id}/{repo_id}"): "project_scoped",
    ("GET", "/v1/projects/{project_id}/sources/scope"): "project_scoped",
    # INPROOM-110: unauthenticated, HMAC-signed webhook receiver (covered by test_webhooks_integration.py)
    ("POST", "/v1/hooks/github/{tenant_id}/{connection_id}"): "webhook",
}

SCOPED = [
    r for r, kind in ROUTES.items() if kind in ("workspace_scoped", "project_scoped", "connection_scoped")
]
AUTHENTICATED = [r for r, kind in ROUTES.items() if kind not in ("public", "webhook")]


def app_routes() -> set[tuple[str, str]]:
    found = set()
    for route in create_app().routes:
        if isinstance(route, APIRoute):
            for method in route.methods - {"HEAD", "OPTIONS"}:
                found.add((method, route.path))
    return found


def test_every_route_is_classified():
    unclassified = app_routes() - ROUTES.keys()
    stale = ROUTES.keys() - app_routes()
    assert not unclassified, f"Classify these routes in ROUTES and cover them: {sorted(unclassified)}"
    assert not stale, f"ROUTES lists routes that no longer exist: {sorted(stale)}"


@dataclass
class Tenant:
    id: uuid.UUID
    headers: dict
    workspace: dict
    project: dict
    connection: dict

    def secrets(self) -> list[str]:
        """Identifiers and names that must never appear in another tenant's responses."""
        return [
            str(self.id),
            self.workspace["id"],
            self.workspace["slug"],
            self.workspace["name"],
            self.project["id"],
            self.project["key"],
            self.project["name"],
            self.connection["id"],
            self.connection["display_name"],
        ]


@pytest.fixture
def world(db_client, tokens, fake_github):
    def make(label: str) -> Tenant:
        tid = uuid.uuid4()
        h = bearer(tokens.mint(tenant_id=tid, user_id=f"admin-{label}", roles=["tenant_admin"]))
        suffix = uuid.uuid4().hex[:6]
        ws = db_client.post(
            "/v1/workspaces",
            json={"slug": f"{label}-{suffix}", "name": f"Secret workspace {label}{suffix}"},
            headers=h,
        ).json()
        key = f"{label.upper()}{suffix[:4].upper()}"
        pj = db_client.post(
            f"/v1/workspaces/{ws['id']}/projects",
            json={"key": key, "name": f"Secret project {label}{suffix}", "mode": "NEW"},
            headers=h,
        ).json()
        conn = db_client.post(
            "/v1/connections",
            json={"display_name": f"Secret GitHub {label}{suffix}", "token": VALID_TOKEN},
            headers=h,
        ).json()
        selected = db_client.put(
            f"/v1/projects/{pj['id']}/sources/repositories/{conn['id']}/1",
            json={"branches": ["main"]},
            headers=h,
        )
        assert selected.status_code == 200, selected.text
        return Tenant(tid, h, ws, pj, conn)

    return make("a"), make("b")


def _request(client, method, path_template, tenant_ids: Tenant, headers):
    path = path_template.format(
        workspace_id=tenant_ids.workspace["id"],
        project_id=tenant_ids.project["id"],
        user_id=tenant_ids.project.get("owner_id", "someone"),  # a real member of the victim project
        version=1,
        connection_id=tenant_ids.connection["id"],
        repo_id="1",
    )
    if path_template.endswith("/repositories") and "{connection_id}" in path_template:
        path += f"?project_id={tenant_ids.project['id']}"
    kwargs: dict = {"headers": dict(headers)}
    if path_template == "/v1/connections" and method == "POST":
        kwargs["json"] = {"display_name": f"x-{uuid.uuid4().hex[:6]}", "token": VALID_TOKEN}
    elif method == "PUT" and path_template.endswith("/credential"):
        kwargs["json"] = {"token": VALID_TOKEN}
    elif method == "PUT" and "/sources/repositories/" in path_template:
        kwargs["json"] = {"branches": ["main"]}
    elif method == "PUT" and "/members/" in path_template:
        kwargs["json"] = {"roles": ["viewer"]}
    elif method == "PUT" and path_template.endswith("/settings"):
        kwargs["json"] = {"settings": {"write_mode": "draft_only"}}
        kwargs["headers"]["If-Match"] = '"1"'
    elif method == "POST" and path_template.endswith("/lifecycle"):
        kwargs["json"] = {"action": "archive"}
    elif method == "POST" and path_template.endswith("/projects"):
        kwargs["json"] = {"key": "XTENANT", "name": "Cross tenant write", "mode": "NEW"}
    elif method == "PATCH":
        kwargs["json"] = {"name": "Cross tenant rename"}
        kwargs["headers"]["If-Match"] = f'"{tenant_ids.project["version"]}"'
    return client.request(method, path, **kwargs)


def _assert_no_leak(response, victim: Tenant):
    blob = response.text + json.dumps(dict(response.headers))
    leaked = [s for s in victim.secrets() if s in blob]
    assert not leaked, f"response leaked {leaked}"


@pytest.mark.parametrize(("method", "path"), SCOPED, ids=[f"{m} {p}" for m, p in SCOPED])
def test_foreign_tenant_ids_return_404_without_leaking(db_client, world, method, path):
    a, b = world
    r = _request(db_client, method, path, a, b.headers)  # tenant B uses tenant A's ids
    record(f"{method} {path}", "tenant B token + tenant A ids", "404", str(r.status_code))
    assert r.status_code == 404, r.text
    assert r.json()["code"] == "NOT_FOUND"
    _assert_no_leak(r, a)


@pytest.mark.parametrize(("method", "path"), SCOPED, ids=[f"{m} {p}" for m, p in SCOPED])
def test_foreign_response_is_identical_to_nonexistent_resource(db_client, world, method, path):
    a, b = world
    ghost = Tenant(
        a.id,
        {},
        {"id": str(uuid.uuid4())},
        {"id": str(uuid.uuid4()), "version": 1},
        {"id": str(uuid.uuid4())},
    )
    foreign = _request(db_client, method, path, a, b.headers)
    missing = _request(db_client, method, path, ghost, b.headers)

    def strip(resp):
        return resp.status_code, {k: v for k, v in resp.json().items() if k != "trace_id"}

    same = strip(foreign) == strip(missing)
    record(
        f"{method} {path}",
        "existence probe (foreign vs random id)",
        "identical",
        "identical" if same else "differs",
    )
    assert same


def test_cross_tenant_attempts_do_not_modify_victim_data(db_client, world, owner_engine):
    a, b = world
    for method, path in SCOPED:
        _request(db_client, method, path, a, b.headers)
    after = db_client.get(f"/v1/projects/{a.project['id']}", headers=a.headers).json()
    assert (after["name"], after["status"], after["version"]) == (a.project["name"], "ACTIVE", 1)
    with owner_engine.connect() as conn:
        a_projects = conn.execute(
            text("SELECT count(*) FROM projects WHERE tenant_id=:t"), {"t": a.id}
        ).scalar()
        b_projects = conn.execute(
            text("SELECT count(*) FROM projects WHERE tenant_id=:t"), {"t": b.id}
        ).scalar()
    assert (a_projects, b_projects) == (1, 1)
    record("all workspace/project routes", "victim data after attacks", "unchanged", "unchanged")


def test_collections_never_contain_other_tenants_items(db_client, world):
    a, b = world
    for viewer, victim in ((a, b), (b, a)):
        r = db_client.get("/v1/workspaces?limit=100", headers=viewer.headers)
        ids = {w["id"] for w in r.json()["items"]}
        assert viewer.workspace["id"] in ids and victim.workspace["id"] not in ids
        _assert_no_leak(r, victim)
        own = db_client.get(f"/v1/workspaces/{viewer.workspace['id']}/projects", headers=viewer.headers)
        _assert_no_leak(own, victim)
    record("GET /v1/workspaces", "list as other tenant", "own items only", "own items only")
    record(
        "GET /v1/workspaces/{workspace_id}/projects", "list own workspace", "own items only", "own items only"
    )


def test_uniqueness_does_not_leak_across_tenants(db_client, world):
    """Tenant B can reuse tenant A's slug and key; a 409 would reveal that A uses them."""
    a, b = world
    ws = db_client.post(
        "/v1/workspaces", json={"slug": a.workspace["slug"], "name": "Mine"}, headers=b.headers
    )
    pj = db_client.post(
        f"/v1/workspaces/{b.workspace['id']}/projects",
        json={"key": a.project["key"], "name": "Mine too"},
        headers=b.headers,
    )
    record("POST /v1/workspaces", "reuse tenant A's slug", "201", str(ws.status_code))
    record("POST /v1/workspaces/{workspace_id}/projects", "reuse tenant A's key", "201", str(pj.status_code))
    assert ws.status_code == 201 and pj.status_code == 201


def test_me_reports_only_the_callers_tenant(db_client, world):
    a, b = world
    r = db_client.get("/v1/me", headers=b.headers)
    record(
        "GET /v1/me",
        "tenant B token",
        "tenant B",
        "tenant B" if r.json()["tenant_id"] == str(b.id) else "other",
    )
    assert r.json()["tenant_id"] == str(b.id)
    _assert_no_leak(r, a)


def _forge_tid(token: str, tenant_id: uuid.UUID) -> str:
    header, payload, signature = token.split(".")
    claims = json.loads(base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4)))
    claims["tid"] = str(tenant_id)
    forged = base64.urlsafe_b64encode(json.dumps(claims).encode()).rstrip(b"=").decode()
    return f"{header}.{forged}.{signature}"


@pytest.mark.parametrize(("method", "path"), AUTHENTICATED, ids=[f"{m} {p}" for m, p in AUTHENTICATED])
def test_forged_tenant_claim_is_rejected_on_every_authenticated_route(db_client, world, method, path):
    a, b = world
    forged = _forge_tid(b.headers["Authorization"].removeprefix("Bearer "), a.id)
    r = _request(db_client, method, path, a, {"Authorization": f"Bearer {forged}"})
    record(f"{method} {path}", "tenant B token with tid forged to A", "401", str(r.status_code))
    assert r.status_code == 401 and r.json()["code"] == "TOKEN_INVALID_SIGNATURE"
    _assert_no_leak(r, a)


@pytest.mark.parametrize(("method", "path"), AUTHENTICATED, ids=[f"{m} {p}" for m, p in AUTHENTICATED])
def test_unauthenticated_requests_are_rejected(db_client, world, method, path):
    a, _ = world
    r = _request(db_client, method, path, a, {})
    record(f"{method} {path}", "no token", "401", str(r.status_code))
    assert r.status_code == 401
    _assert_no_leak(r, a)


def test_public_routes_expose_no_tenant_data(db_client, world):
    a, b = world
    for method, path in (r for r, kind in ROUTES.items() if kind == "public"):
        r = db_client.request(method, path)
        _assert_no_leak(r, a)
        _assert_no_leak(r, b)
        record(f"{method} {path}", "unauthenticated read", "no tenant data", "no tenant data")


def test_database_hides_victim_rows_from_attacker_session(world, owner_engine):
    a, b = world
    with tenant_session(b.id) as conn:
        for table in TENANT_SCOPED_TABLES:
            column = "id" if table == "tenants" else "tenant_id"
            visible = conn.execute(
                text(f"SELECT count(*) FROM {table} WHERE {column} = :a"), {"a": a.id}
            ).scalar()
            record(f"DB {table}", "tenant B session selects tenant A rows", "0 rows", f"{visible} rows")
            assert visible == 0, table
    # Sanity: the rows really exist (seen by the owner, who bypasses RLS).
    with owner_engine.connect() as conn:
        assert (
            conn.execute(text("SELECT count(*) FROM projects WHERE tenant_id=:a"), {"a": a.id}).scalar() == 1
        )


@pytest.mark.parametrize(("method", "path"), SCOPED, ids=[f"{m} {p}" for m, p in SCOPED])
def test_cross_tenant_probe_is_audited_in_callers_tenant_only(db_client, world, owner_engine, method, path):
    """INPROOM-90 (QA-01): every masked 404 leaves one DENIED audit row in the caller's tenant,
    naming only what the caller sent; the victim's audit trail is untouched."""
    a, b = world
    if "{project_id}" in path:
        target = a.project["id"]
    elif "{connection_id}" in path:
        target = a.connection["id"]
    else:
        target = a.workspace["id"]
    with owner_engine.connect() as conn:
        victim_before = conn.execute(
            text("SELECT count(*) FROM audit_events WHERE tenant_id=:a"), {"a": a.id}
        ).scalar()
    r = _request(db_client, method, path, a, b.headers)
    with owner_engine.connect() as conn:
        rows = (
            conn.execute(
                text("SELECT * FROM audit_events WHERE tenant_id=:b AND target_id=:t AND outcome='DENIED'"),
                {"b": b.id, "t": target},
            )
            .mappings()
            .all()
        )
        victim_after = conn.execute(
            text("SELECT count(*) FROM audit_events WHERE tenant_id=:a"), {"a": a.id}
        ).scalar()
    record(
        f"{method} {path}",
        "foreign id probe audited (caller tenant)",
        "1 DENIED row",
        f"{len(rows)} DENIED row",
    )
    assert r.status_code == 404
    assert len(rows) == 1
    row = rows[0]
    assert row["actor_id"] == "admin-b" and row["reason"] == "resource not found or not accessible"
    assert row["trace_id"] == r.headers["X-Trace-Id"]
    assert row["project_id"] is None  # nothing about tenant A is recorded beyond the id the caller sent
    assert victim_after == victim_before


def test_missing_and_foreign_ids_are_audited_identically(db_client, world, owner_engine):
    a, b = world
    ghost = str(uuid.uuid4())
    foreign = db_client.get(f"/v1/projects/{a.project['id']}", headers=b.headers)
    missing = db_client.get(f"/v1/projects/{ghost}", headers=b.headers)
    with owner_engine.connect() as conn:
        rows = (
            conn.execute(
                text(
                    "SELECT target_id, action, outcome, reason FROM audit_events "
                    "WHERE tenant_id=:b AND target_id IN (:f, :g)"
                ),
                {"b": b.id, "f": a.project["id"], "g": ghost},
            )
            .mappings()
            .all()
        )
    shapes = {(r["action"], r["outcome"], r["reason"]) for r in rows}
    record(
        "GET /v1/projects/{project_id}",
        "audit shape foreign vs random id",
        "identical",
        "identical" if len(shapes) == 1 else "differs",
    )
    assert foreign.status_code == missing.status_code == 404
    assert len(rows) == 2 and len(shapes) == 1
