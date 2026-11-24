"""Jira source connector (INPROOM-130 / INPROOM-23): connections, project selections, schema snapshots.

Revision ID: 0005_jira_sources
Revises: 0004_git_sources
Create Date: 2026-11-24

source_connections now also holds Jira connections (provider/kind 'jira'; Cloud email + API token as
'basic_token', Data Center personal access token as 'pat'). The token itself lives only in connection_secrets.
New tables are tenant-scoped with FORCE ROW LEVEL SECURITY and the tenant_isolation policy, like 0001/0004.
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB, UUID

revision = "0005_jira_sources"
down_revision = "0004_git_sources"
branch_labels = None
depends_on = None

APP_ROLE = "inproom_app"
TABLES = {
    "project_jira_sources": "SELECT, INSERT, UPDATE, DELETE",
    "jira_schema_snapshots": "SELECT, INSERT, DELETE",
}
CONSTRAINTS = {
    "ck_source_connections_provider": ("provider IN ('github')", "provider IN ('github','jira')"),
    "ck_source_connections_kind": ("kind IN ('git')", "kind IN ('git','jira')"),
    "ck_source_connections_auth": (
        "auth_type IN ('token','github_app')",
        "auth_type IN ('token','github_app','basic_token','pat')",
    ),
}


def _ts(name: str) -> sa.Column:
    return sa.Column(name, sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now())


def _swap_constraints(new: bool) -> None:
    for name, (old_sql, new_sql) in CONSTRAINTS.items():
        op.drop_constraint(name, "source_connections", type_="check")
        op.create_check_constraint(name, "source_connections", new_sql if new else old_sql)


def upgrade() -> None:
    _swap_constraints(new=True)
    op.create_table(
        "project_jira_sources",
        sa.Column("id", UUID(as_uuid=True), primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column("tenant_id", UUID(as_uuid=True), nullable=False),
        sa.Column("project_id", UUID(as_uuid=True), nullable=False),
        sa.Column("connection_id", UUID(as_uuid=True), nullable=False),
        sa.Column("jira_project_id", sa.Text, nullable=False),
        sa.Column("jira_key", sa.Text, nullable=False),
        sa.Column("name", sa.Text, nullable=False),
        sa.Column("status", sa.Text, nullable=False),
        sa.Column("status_reason", sa.Text),
        sa.Column("capabilities", JSONB, nullable=False, server_default=sa.text("'{}'::jsonb")),
        sa.Column("mapping", JSONB, nullable=False, server_default=sa.text("'{}'::jsonb")),
        sa.Column("mapping_version", sa.Integer, nullable=False, server_default="1"),
        sa.Column("snapshot_version", sa.Integer, nullable=False, server_default="0"),
        sa.Column("warnings", JSONB, nullable=False, server_default=sa.text("'[]'::jsonb")),
        sa.Column("selected_by", sa.Text, nullable=False),
        _ts("selected_at"),
        _ts("updated_at"),
        sa.Column("version", sa.Integer, nullable=False, server_default="1"),
        sa.UniqueConstraint("tenant_id", "id", name="uq_project_jira_sources_tenant_id"),
        sa.UniqueConstraint(
            "project_id", "connection_id", "jira_project_id", name="uq_project_jira_sources_project"
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "project_id"], ["projects.tenant_id", "projects.id"], name="fk_jira_sources_project"
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "connection_id"],
            ["source_connections.tenant_id", "source_connections.id"],
            name="fk_jira_sources_connection",
        ),
        sa.CheckConstraint("status IN ('ACTIVE','PAUSED')", name="ck_project_jira_sources_status"),
    )
    op.create_table(
        "jira_schema_snapshots",
        sa.Column("id", UUID(as_uuid=True), primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column("tenant_id", UUID(as_uuid=True), nullable=False),
        sa.Column("source_id", UUID(as_uuid=True), nullable=False),
        sa.Column("version", sa.Integer, nullable=False),
        sa.Column("fingerprint", sa.Text, nullable=False),
        sa.Column("schema", JSONB, nullable=False),
        sa.Column("diff", JSONB, nullable=False, server_default=sa.text("'{}'::jsonb")),
        sa.Column("discovered_by", sa.Text, nullable=False),
        _ts("discovered_at"),
        sa.UniqueConstraint("source_id", "version", name="uq_jira_schema_snapshots_version"),
        sa.ForeignKeyConstraint(
            ["tenant_id", "source_id"],
            ["project_jira_sources.tenant_id", "project_jira_sources.id"],
            name="fk_jira_snapshots_source",
            ondelete="CASCADE",
        ),
        sa.CheckConstraint("version >= 1", name="ck_jira_schema_snapshots_version"),
    )
    for table, grants in TABLES.items():
        op.execute(f"GRANT {grants} ON {table} TO {APP_ROLE}")
        op.execute(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY")
        op.execute(f"ALTER TABLE {table} FORCE ROW LEVEL SECURITY")
        op.execute(
            f"CREATE POLICY tenant_isolation ON {table} "
            "USING (tenant_id = current_tenant()) WITH CHECK (tenant_id = current_tenant())"
        )


def downgrade() -> None:
    jira_rows = (
        op.get_bind()
        .execute(sa.text("SELECT count(*) FROM source_connections WHERE provider <> 'github'"))
        .scalar()
    )
    if jira_rows:
        raise RuntimeError(
            f"refusing to downgrade: {jira_rows} Jira connection(s) exist; revoke and delete them first"
        )
    for table in reversed(list(TABLES)):
        op.execute(f"REVOKE ALL ON {table} FROM {APP_ROLE}")
        op.drop_table(table)
    _swap_constraints(new=False)
