"""Git source connectors (INPROOM-108): connections, secrets, project repositories, webhook deliveries.

Revision ID: 0004_git_sources
Revises: 0003_settings_v2
Create Date: 2026-11-23

Every table is tenant-scoped with FORCE ROW LEVEL SECURITY and the tenant_isolation policy, like 0001.
connection_secrets holds only AES-256-GCM ciphertext (app.connectors.vault); plaintext is never stored.
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import ARRAY, JSONB, UUID

revision = "0004_git_sources"
down_revision = "0003_settings_v2"
branch_labels = None
depends_on = None

APP_ROLE = "inproom_app"
TABLES = {
    "source_connections": "SELECT, INSERT, UPDATE",
    "connection_secrets": "SELECT, INSERT, UPDATE, DELETE",
    "project_repositories": "SELECT, INSERT, UPDATE, DELETE",
    "webhook_deliveries": "SELECT, INSERT",
}


def _id() -> sa.Column:
    return sa.Column("id", UUID(as_uuid=True), primary_key=True, server_default=sa.text("gen_random_uuid()"))


def _ts(name: str, nullable: bool = False) -> sa.Column:
    return sa.Column(
        name,
        sa.DateTime(timezone=True),
        nullable=nullable,
        server_default=None if nullable else sa.func.now(),
    )


def upgrade() -> None:
    op.create_table(
        "source_connections",
        _id(),
        sa.Column("tenant_id", UUID(as_uuid=True), sa.ForeignKey("tenants.id"), nullable=False),
        sa.Column("provider", sa.Text, nullable=False),
        sa.Column("kind", sa.Text, nullable=False),
        sa.Column("display_name", sa.Text, nullable=False),
        sa.Column("base_url", sa.Text, nullable=False),
        sa.Column("auth_type", sa.Text, nullable=False),
        sa.Column("account_login", sa.Text),
        sa.Column("granted_scopes", ARRAY(sa.Text), nullable=False, server_default=sa.text("'{}'::text[]")),
        sa.Column("credential_fingerprint", sa.Text),
        sa.Column("status", sa.Text, nullable=False),
        sa.Column("health", JSONB, nullable=False, server_default=sa.text("'{}'::jsonb")),
        _ts("last_checked_at", nullable=True),
        sa.Column("created_by", sa.Text, nullable=False),
        _ts("created_at"),
        _ts("updated_at"),
        sa.Column("version", sa.Integer, nullable=False, server_default="1"),
        sa.UniqueConstraint("tenant_id", "id", name="uq_source_connections_tenant_id"),
        sa.UniqueConstraint("tenant_id", "display_name", name="uq_source_connections_name"),
        sa.CheckConstraint("provider IN ('github')", name="ck_source_connections_provider"),
        sa.CheckConstraint("kind IN ('git')", name="ck_source_connections_kind"),
        sa.CheckConstraint("auth_type IN ('token','github_app')", name="ck_source_connections_auth"),
        sa.CheckConstraint(
            "status IN ('ACTIVE','DEGRADED','INVALID','REVOKED')", name="ck_source_connections_status"
        ),
    )
    op.create_table(
        "connection_secrets",
        sa.Column("tenant_id", UUID(as_uuid=True), nullable=False),
        sa.Column("connection_id", UUID(as_uuid=True), nullable=False),
        sa.Column("purpose", sa.Text, nullable=False),
        sa.Column("key_id", sa.Text, nullable=False),
        sa.Column("nonce", sa.LargeBinary, nullable=False),
        sa.Column("ciphertext", sa.LargeBinary, nullable=False),
        _ts("created_at"),
        sa.PrimaryKeyConstraint("connection_id", "purpose", name="pk_connection_secrets"),
        sa.ForeignKeyConstraint(
            ["tenant_id", "connection_id"],
            ["source_connections.tenant_id", "source_connections.id"],
            name="fk_secrets_connection",
        ),
        sa.CheckConstraint("purpose IN ('credential','webhook')", name="ck_connection_secrets_purpose"),
    )
    op.create_table(
        "project_repositories",
        _id(),
        sa.Column("tenant_id", UUID(as_uuid=True), nullable=False),
        sa.Column("project_id", UUID(as_uuid=True), nullable=False),
        sa.Column("connection_id", UUID(as_uuid=True), nullable=False),
        sa.Column("provider_repo_id", sa.Text, nullable=False),
        sa.Column("full_name", sa.Text, nullable=False),
        sa.Column("private", sa.Boolean, nullable=False),
        sa.Column("default_branch", sa.Text, nullable=False),
        sa.Column("branches", ARRAY(sa.Text), nullable=False),
        sa.Column("permissions", JSONB, nullable=False, server_default=sa.text("'{}'::jsonb")),
        sa.Column("status", sa.Text, nullable=False),
        sa.Column("status_reason", sa.Text),
        sa.Column("webhook_id", sa.Text),
        sa.Column("webhook_status", sa.Text, nullable=False, server_default="none"),
        sa.Column("selected_by", sa.Text, nullable=False),
        _ts("selected_at"),
        _ts("updated_at"),
        sa.Column("version", sa.Integer, nullable=False, server_default="1"),
        sa.UniqueConstraint(
            "project_id", "connection_id", "provider_repo_id", name="uq_project_repositories_repo"
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "project_id"],
            ["projects.tenant_id", "projects.id"],
            name="fk_project_repos_project",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "connection_id"],
            ["source_connections.tenant_id", "source_connections.id"],
            name="fk_project_repos_connection",
        ),
        sa.CheckConstraint("status IN ('ACTIVE','PAUSED')", name="ck_project_repositories_status"),
        sa.CheckConstraint(
            "webhook_status IN ('none','registered','failed','not_configured')",
            name="ck_project_repositories_webhook",
        ),
        sa.CheckConstraint("cardinality(branches) BETWEEN 1 AND 50", name="ck_project_repositories_branches"),
    )
    op.create_table(
        "webhook_deliveries",
        sa.Column("tenant_id", UUID(as_uuid=True), nullable=False),
        sa.Column("connection_id", UUID(as_uuid=True), nullable=False),
        sa.Column("delivery_id", sa.Text, nullable=False),
        sa.Column("event", sa.Text, nullable=False),
        sa.Column("provider_repo_id", sa.Text),
        sa.Column("outcome", sa.Text, nullable=False),
        _ts("received_at"),
        sa.PrimaryKeyConstraint("connection_id", "delivery_id", name="pk_webhook_deliveries"),
        sa.ForeignKeyConstraint(
            ["tenant_id", "connection_id"],
            ["source_connections.tenant_id", "source_connections.id"],
            name="fk_deliveries_connection",
        ),
        sa.CheckConstraint("outcome IN ('processed','ignored')", name="ck_webhook_deliveries_outcome"),
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
    for table in reversed(list(TABLES)):
        op.execute(f"REVOKE ALL ON {table} FROM {APP_ROLE}")
        op.drop_table(table)
