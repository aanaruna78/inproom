"""Tenancy, projects, audit, outbox and idempotency tables with forced row-level security.

Revision ID: 0001_tenancy_projects
Revises: 0000_baseline
Create Date: 2026-11-23

Isolation model
- The API connects as the migration owner but runs every request transaction as
  `SET LOCAL ROLE inproom_app` + `set_config('app.tenant_id', <tid>, true)`.
- inproom_app is NOLOGIN and not a table owner; FORCE ROW LEVEL SECURITY makes the
  policies apply to it unconditionally. No tenant set => current_tenant() is NULL => zero rows.
- audit_events is append-only for inproom_app (INSERT + SELECT only).
- inproom_app is a cluster-wide role, so downgrade revokes its grants but does not drop it.
"""

from alembic import op

from migrations.snapshots.schema_0001 import APP_ROLE, TENANT_SCOPED_TABLES, metadata

revision = "0001_tenancy_projects"
down_revision = "0000_baseline"
branch_labels = None
depends_on = None

# Column that holds the tenant id for each table's policy.
TENANT_COLUMN = {name: "tenant_id" for name in TENANT_SCOPED_TABLES} | {"tenants": "id"}

# Privileges for the application role. Anything not listed is denied.
GRANTS = {
    "tenants": "SELECT, INSERT, UPDATE",
    "workspaces": "SELECT, INSERT, UPDATE",
    "projects": "SELECT, INSERT, UPDATE",
    "project_members": "SELECT, INSERT, DELETE",
    "project_settings_versions": "SELECT, INSERT",
    "audit_events": "SELECT, INSERT",
    "outbox_events": "SELECT, INSERT, UPDATE",
    "idempotency_keys": "SELECT, INSERT, UPDATE, DELETE",
}


def upgrade() -> None:
    bind = op.get_bind()
    metadata.create_all(bind, tables=[metadata.tables[name] for name in TENANT_SCOPED_TABLES])

    op.execute(
        f"""
        DO $$
        BEGIN
          IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = '{APP_ROLE}') THEN
            CREATE ROLE {APP_ROLE} NOLOGIN NOBYPASSRLS;
          END IF;
        END $$;
        """
    )
    # Lets a non-superuser migration owner `SET ROLE inproom_app` in production deployments.
    op.execute(f"GRANT {APP_ROLE} TO CURRENT_USER")
    op.execute(f"GRANT USAGE ON SCHEMA public TO {APP_ROLE}")
    op.execute(
        """
        CREATE OR REPLACE FUNCTION current_tenant() RETURNS uuid
        LANGUAGE sql STABLE AS
        $$ SELECT NULLIF(current_setting('app.tenant_id', true), '')::uuid $$
        """
    )
    op.execute(f"GRANT EXECUTE ON FUNCTION current_tenant() TO {APP_ROLE}")

    for table in TENANT_SCOPED_TABLES:
        column = TENANT_COLUMN[table]
        op.execute(f"GRANT {GRANTS[table]} ON {table} TO {APP_ROLE}")
        op.execute(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY")
        op.execute(f"ALTER TABLE {table} FORCE ROW LEVEL SECURITY")
        op.execute(
            f"CREATE POLICY tenant_isolation ON {table} "
            f"USING ({column} = current_tenant()) WITH CHECK ({column} = current_tenant())"
        )


def downgrade() -> None:
    for table in reversed(TENANT_SCOPED_TABLES):
        op.execute(f"REVOKE ALL ON {table} FROM {APP_ROLE}")
    bind = op.get_bind()
    # Tables first: their policies depend on current_tenant().
    metadata.drop_all(bind, tables=[metadata.tables[name] for name in TENANT_SCOPED_TABLES])
    op.execute(f"REVOKE EXECUTE ON FUNCTION current_tenant() FROM {APP_ROLE}")
    op.execute("DROP FUNCTION current_tenant()")
    op.execute(f"REVOKE USAGE ON SCHEMA public FROM {APP_ROLE}")
