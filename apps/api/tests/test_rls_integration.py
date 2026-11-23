"""AC1-3: migration round trip, row-level security and audit immutability against real PostgreSQL."""

import uuid
from pathlib import Path

import pytest
from alembic import command
from alembic.autogenerate import compare_metadata
from alembic.config import Config
from alembic.migration import MigrationContext
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.engine import make_url
from sqlalchemy.exc import DBAPIError, ProgrammingError

from app.db import tenant_session
from app.db_schema import TENANT_SCOPED_TABLES, metadata

pytestmark = pytest.mark.integration

API_DIR = Path(__file__).resolve().parents[1]


def _alembic(url: str) -> Config:
    cfg = Config(str(API_DIR / "alembic.ini"))
    cfg.set_main_option("script_location", str(API_DIR / "migrations"))
    cfg.set_main_option("sqlalchemy.url", url)
    return cfg


@pytest.fixture
def scratch_database(test_database_url):
    """A throwaway database so the round trip never touches shared data."""
    name = f"inproom_mig_{uuid.uuid4().hex[:10]}"
    admin = create_engine(test_database_url, isolation_level="AUTOCOMMIT")
    with admin.connect() as conn:
        conn.execute(text(f'CREATE DATABASE "{name}"'))
    url = make_url(test_database_url).set(database=name).render_as_string(hide_password=False)
    yield url
    with admin.connect() as conn:
        conn.execute(text(f'DROP DATABASE "{name}" WITH (FORCE)'))
    admin.dispose()


def _tables(url: str) -> set[str]:
    engine = create_engine(url)
    try:
        return set(inspect(engine).get_table_names())
    finally:
        engine.dispose()


def test_migration_upgrade_downgrade_upgrade(scratch_database):
    cfg = _alembic(scratch_database)
    command.upgrade(cfg, "head")
    assert set(TENANT_SCOPED_TABLES) <= _tables(scratch_database)
    command.downgrade(cfg, "0000_baseline")
    assert not set(TENANT_SCOPED_TABLES) & _tables(scratch_database)
    command.upgrade(cfg, "head")
    assert set(TENANT_SCOPED_TABLES) <= _tables(scratch_database)


def test_code_schema_matches_migrated_database(owner_engine):
    """app/db_schema.py must describe exactly what the migrations created (no drift)."""
    with owner_engine.connect() as conn:
        diff = compare_metadata(MigrationContext.configure(conn), metadata)
    relevant = [
        d
        for d in diff
        if not (isinstance(d, tuple) and d[0] == "remove_table" and d[1].name == "alembic_version")
    ]
    assert relevant == [], relevant


def test_rls_forced_and_policy_present_on_every_table(owner_engine):
    with owner_engine.connect() as conn:
        rows = conn.execute(
            text(
                "SELECT c.relname, c.relrowsecurity, c.relforcerowsecurity, "
                "(SELECT count(*) FROM pg_policy p WHERE p.polrelid = c.oid) AS policies "
                "FROM pg_class c WHERE c.relname = ANY(:t)"
            ),
            {"t": list(TENANT_SCOPED_TABLES)},
        ).all()
    assert {r.relname for r in rows} == set(TENANT_SCOPED_TABLES)
    for r in rows:
        assert r.relrowsecurity and r.relforcerowsecurity and r.policies == 1, r


SEED_SQL = [
    "INSERT INTO workspaces (id, tenant_id, slug, name, created_by) "
    "VALUES (:w, :t, 'ws-main', 'WS', 'seed')",
    "INSERT INTO projects (id, tenant_id, workspace_id, key, name, mode, status, owner_id) "
    "VALUES (:p, :t, :w, :k, 'Project', 'NEW', 'ACTIVE', 'seed')",
    "INSERT INTO project_members (tenant_id, project_id, user_id, role, added_by) "
    "VALUES (:t, :p, 'seed', 'project_admin', 'seed')",
    "INSERT INTO project_settings_versions "
    "(tenant_id, project_id, version, settings, content_sha256, created_by) "
    "VALUES (:t, :p, 1, '{}', 'x', 'seed')",
    "INSERT INTO audit_events (tenant_id, actor_id, action, target_type, outcome) "
    "VALUES (:t, 'seed', 'seed', 'x', 'SUCCESS')",
    "INSERT INTO outbox_events (tenant_id, type, payload) VALUES (:t, 'seed', '{}')",
    "INSERT INTO idempotency_keys (tenant_id, key, request_sha256, status) "
    "VALUES (:t, 'k', 'x', 'IN_PROGRESS')",
    "INSERT INTO tenant_users (tenant_id, user_id, email) VALUES (:t, 'seed', 'seed@example.test')",
    "INSERT INTO source_connections (id, tenant_id, provider, kind, display_name, base_url, auth_type, "
    "status, created_by) "
    "VALUES (:c, :t, 'github', 'git', 'gh', 'https://api.github.com', 'token', 'ACTIVE', 'seed')",
    "INSERT INTO connection_secrets (tenant_id, connection_id, purpose, key_id, nonce, ciphertext) "
    "VALUES (:t, :c, 'credential', 'k1', '\\x00', '\\x00')",
    "INSERT INTO project_repositories (tenant_id, project_id, connection_id, provider_repo_id, full_name, "
    "private, default_branch, branches, status, selected_by) "
    "VALUES (:t, :p, :c, '1', 'acme/api', true, 'main', ARRAY['main'], 'ACTIVE', 'seed')",
    "INSERT INTO webhook_deliveries (tenant_id, connection_id, delivery_id, event, outcome) "
    "VALUES (:t, :c, 'd-1', 'push', 'processed')",
]


def _seed(owner_engine, tenant_id: uuid.UUID) -> dict[str, str]:
    """One row in every tenant-scoped table for tenant_id (inserted as owner, bypassing RLS)."""
    params = {
        "t": tenant_id,
        "w": uuid.uuid4(),
        "p": uuid.uuid4(),
        "c": uuid.uuid4(),
        "k": "P" + uuid.uuid4().hex[:6].upper(),
    }
    with owner_engine.begin() as c:
        for sql in SEED_SQL:
            c.execute(text(sql), params)
    return {"workspace": str(params["w"]), "project": str(params["p"])}


def _tenant_column(table: str) -> str:
    return "id" if table == "tenants" else "tenant_id"


def test_each_tenant_sees_only_its_own_rows_in_every_table(owner_engine, make_tenant):
    a, b = make_tenant("A"), make_tenant("B")
    _seed(owner_engine, a)
    _seed(owner_engine, b)
    for viewer, other in ((a, b), (b, a)):
        with tenant_session(viewer) as conn:
            for table in TENANT_SCOPED_TABLES:
                col = _tenant_column(table)
                seen = set(conn.execute(text(f"SELECT DISTINCT {col} FROM {table}")).scalars())
                assert seen == {viewer}, (table, seen)
                leaked = conn.execute(
                    text(f"SELECT count(*) FROM {table} WHERE {col} = :o"), {"o": other}
                ).scalar()
                assert leaked == 0, table


def test_no_tenant_set_means_zero_rows(owner_engine, make_tenant):
    _seed(owner_engine, make_tenant())
    with owner_engine.begin() as conn:
        conn.execute(text("SET LOCAL ROLE inproom_app"))
        for table in TENANT_SCOPED_TABLES:
            assert conn.execute(text(f"SELECT count(*) FROM {table}")).scalar() == 0, table


def test_cannot_insert_rows_for_another_tenant(owner_engine, make_tenant):
    a, b = make_tenant(), make_tenant()
    with pytest.raises(ProgrammingError, match="row-level security"):
        with tenant_session(a) as conn:
            conn.execute(
                text("INSERT INTO outbox_events (tenant_id, type, payload) VALUES (:b,'x','{}')"), {"b": b}
            )


def test_cannot_move_a_row_to_another_tenant(owner_engine, make_tenant):
    a, b = make_tenant(), make_tenant()
    _seed(owner_engine, a)
    with pytest.raises(ProgrammingError, match="row-level security"):
        with tenant_session(a) as conn:
            conn.execute(text("UPDATE outbox_events SET tenant_id = :b"), {"b": b})


@pytest.mark.parametrize(
    "statement", ["UPDATE audit_events SET action = 'tampered'", "DELETE FROM audit_events"]
)
def test_audit_events_are_append_only_for_app_role(owner_engine, make_tenant, statement):
    tid = make_tenant()
    _seed(owner_engine, tid)
    with pytest.raises(DBAPIError, match="permission denied"):
        with tenant_session(tid) as conn:
            conn.execute(text(statement))
    with owner_engine.connect() as conn:
        actions = (
            conn.execute(text("SELECT action FROM audit_events WHERE tenant_id = :t"), {"t": tid})
            .scalars()
            .all()
        )
    assert actions == ["seed"]


def test_tenant_session_rejects_non_uuid():
    with pytest.raises(TypeError):
        with tenant_session("a0000000-0000-4000-8000-00000000000a"):  # type: ignore[arg-type]
            pass
