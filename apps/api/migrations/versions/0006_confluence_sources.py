"""Confluence source connector (INPROOM-134 / INPROOM-24): space selections and a versioned page catalog.

Revision ID: 0006_confluence_sources
Revises: 0005_jira_sources
Create Date: 2026-11-24

source_connections now also holds Confluence connections. Page bodies are NOT stored (E03 object storage):
the catalog keeps metadata, ACL metadata, attachment metadata and a content hash per version seen.
New tables are tenant-scoped with FORCE ROW LEVEL SECURITY and the tenant_isolation policy.
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import ARRAY, JSONB, UUID

revision = "0006_confluence_sources"
down_revision = "0005_jira_sources"
branch_labels = None
depends_on = None

APP_ROLE = "inproom_app"
TABLES = {
    "project_confluence_sources": "SELECT, INSERT, UPDATE, DELETE",
    "confluence_pages": "SELECT, INSERT, UPDATE, DELETE",
    "confluence_page_versions": "SELECT, INSERT, DELETE",
}
CONSTRAINTS = {
    "ck_source_connections_provider": (
        "provider IN ('github','jira')",
        "provider IN ('github','jira','confluence')",
    ),
    "ck_source_connections_kind": ("kind IN ('git','jira')", "kind IN ('git','jira','confluence')"),
}


def _ts(name: str, nullable: bool = False) -> sa.Column:
    return sa.Column(
        name,
        sa.DateTime(timezone=True),
        nullable=nullable,
        server_default=None if nullable else sa.func.now(),
    )


def _swap(new: bool) -> None:
    for name, (old_sql, new_sql) in CONSTRAINTS.items():
        op.drop_constraint(name, "source_connections", type_="check")
        op.create_check_constraint(name, "source_connections", new_sql if new else old_sql)


def upgrade() -> None:
    _swap(new=True)
    op.create_table(
        "project_confluence_sources",
        sa.Column("id", UUID(as_uuid=True), primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column("tenant_id", UUID(as_uuid=True), nullable=False),
        sa.Column("project_id", UUID(as_uuid=True), nullable=False),
        sa.Column("connection_id", UUID(as_uuid=True), nullable=False),
        sa.Column("space_id", sa.Text, nullable=False),
        sa.Column("space_key", sa.Text, nullable=False),
        sa.Column("name", sa.Text, nullable=False),
        sa.Column("filters", JSONB, nullable=False, server_default=sa.text("'{}'::jsonb")),
        sa.Column("capabilities", JSONB, nullable=False, server_default=sa.text("'{}'::jsonb")),
        sa.Column("status", sa.Text, nullable=False),
        sa.Column("status_reason", sa.Text),
        sa.Column("warnings", JSONB, nullable=False, server_default=sa.text("'[]'::jsonb")),
        _ts("last_synced_at", nullable=True),
        sa.Column("last_sync", JSONB, nullable=False, server_default=sa.text("'{}'::jsonb")),
        sa.Column("selected_by", sa.Text, nullable=False),
        _ts("selected_at"),
        _ts("updated_at"),
        sa.Column("version", sa.Integer, nullable=False, server_default="1"),
        sa.UniqueConstraint("tenant_id", "id", name="uq_project_confluence_sources_tenant_id"),
        sa.UniqueConstraint(
            "project_id", "connection_id", "space_id", name="uq_project_confluence_sources_space"
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "project_id"],
            ["projects.tenant_id", "projects.id"],
            name="fk_confluence_sources_project",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "connection_id"],
            ["source_connections.tenant_id", "source_connections.id"],
            name="fk_confluence_sources_connection",
        ),
        sa.CheckConstraint("status IN ('ACTIVE','PAUSED')", name="ck_project_confluence_sources_status"),
    )
    op.create_table(
        "confluence_pages",
        sa.Column("tenant_id", UUID(as_uuid=True), nullable=False),
        sa.Column("source_id", UUID(as_uuid=True), nullable=False),
        sa.Column("page_id", sa.Text, nullable=False),
        sa.Column("title", sa.Text, nullable=False),
        sa.Column("parent_page_id", sa.Text),
        sa.Column("ancestors", ARRAY(sa.Text), nullable=False, server_default=sa.text("'{}'::text[]")),
        sa.Column("status", sa.Text, nullable=False),
        sa.Column("status_reason", sa.Text),
        sa.Column("current_version", sa.Integer, nullable=False),
        sa.Column("content_sha256", sa.Text, nullable=False),
        sa.Column("labels", ARRAY(sa.Text), nullable=False, server_default=sa.text("'{}'::text[]")),
        sa.Column("restrictions", JSONB, nullable=False, server_default=sa.text("'{}'::jsonb")),
        sa.Column("attachments", JSONB, nullable=False, server_default=sa.text("'[]'::jsonb")),
        sa.Column("author", sa.Text),
        sa.Column("last_modified_at", sa.Text),
        sa.Column("web_url", sa.Text),
        _ts("first_seen_at"),
        _ts("updated_at"),
        sa.PrimaryKeyConstraint("source_id", "page_id", name="pk_confluence_pages"),
        sa.ForeignKeyConstraint(
            ["tenant_id", "source_id"],
            ["project_confluence_sources.tenant_id", "project_confluence_sources.id"],
            name="fk_confluence_pages_source",
            ondelete="CASCADE",
        ),
        sa.CheckConstraint(
            "status IN ('CURRENT','RESTRICTED','EXCLUDED','DELETED')", name="ck_confluence_pages_status"
        ),
    )
    op.create_table(
        "confluence_page_versions",
        sa.Column("tenant_id", UUID(as_uuid=True), nullable=False),
        sa.Column("source_id", UUID(as_uuid=True), nullable=False),
        sa.Column("page_id", sa.Text, nullable=False),
        sa.Column("version", sa.Integer, nullable=False),
        sa.Column("content_sha256", sa.Text, nullable=False),
        sa.Column("title", sa.Text, nullable=False),
        sa.Column("author", sa.Text),
        sa.Column("created_at", sa.Text),
        sa.Column("message", sa.Text),
        _ts("seen_at"),
        sa.PrimaryKeyConstraint("source_id", "page_id", "version", name="pk_confluence_page_versions"),
        sa.ForeignKeyConstraint(
            ["tenant_id", "source_id"],
            ["project_confluence_sources.tenant_id", "project_confluence_sources.id"],
            name="fk_confluence_versions_source",
            ondelete="CASCADE",
        ),
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
    rows = (
        op.get_bind()
        .execute(sa.text("SELECT count(*) FROM source_connections WHERE provider = 'confluence'"))
        .scalar()
    )
    if rows:
        raise RuntimeError(f"refusing to downgrade: {rows} Confluence connection(s) exist; delete them first")
    for table in reversed(list(TABLES)):
        op.execute(f"REVOKE ALL ON {table} FROM {APP_ROLE}")
        op.drop_table(table)
    _swap(new=False)
