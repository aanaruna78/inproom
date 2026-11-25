"""Migration projects: plan, resumable inventory runs and the Jira issue inventory (INPROOM-151 / INPROOM-19).

Revision ID: 0009_migration
Revises: 0008_source_health
Create Date: 2026-11-25

Inventory rows are keyed by Jira issue id (keys change when issues move or projects are renamed).
All three tables are tenant-scoped with FORCE ROW LEVEL SECURITY and the tenant_isolation policy.
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB, UUID

revision = "0009_migration"
down_revision = "0008_source_health"
branch_labels = None
depends_on = None

APP_ROLE = "inproom_app"
TABLES = {
    "migration_plans": "SELECT, INSERT, UPDATE",
    "migration_runs": "SELECT, INSERT, UPDATE",
    "migration_issues": "SELECT, INSERT, UPDATE",
}
KINDS_OLD = "'git','jira','confluence','connection'"
KINDS_NEW = "'git','jira','confluence','connection','migration'"


def _ts(name: str, nullable: bool = True) -> sa.Column:
    return sa.Column(
        name,
        sa.DateTime(timezone=True),
        nullable=nullable,
        server_default=None if nullable else sa.func.now(),
    )


def _kinds(kinds: str) -> None:
    for table, name in (
        ("sync_jobs", "ck_sync_jobs_kind"),
        ("source_sync_state", "ck_source_sync_state_kind"),
    ):
        op.drop_constraint(name, table, type_="check")
        op.create_check_constraint(name, table, f"source_kind IN ({kinds})")


def upgrade() -> None:
    op.create_table(
        "migration_plans",
        sa.Column("id", UUID(as_uuid=True), primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column("tenant_id", UUID(as_uuid=True), nullable=False),
        sa.Column("project_id", UUID(as_uuid=True), nullable=False),
        sa.Column("jira_source_id", UUID(as_uuid=True), nullable=False),
        sa.Column("import_mode", sa.Text, nullable=False, server_default="jira_authoritative"),
        sa.Column("history_since", sa.Date),
        sa.Column("status", sa.Text, nullable=False, server_default="DRAFT"),
        sa.Column("baseline", JSONB),
        sa.Column("created_by", sa.Text, nullable=False),
        _ts("created_at", nullable=False),
        _ts("updated_at", nullable=False),
        sa.Column("version", sa.Integer, nullable=False, server_default="1"),
        sa.UniqueConstraint("project_id", name="uq_migration_plans_project"),
        sa.UniqueConstraint("tenant_id", "id", name="uq_migration_plans_tenant_id"),
        sa.ForeignKeyConstraint(
            ["tenant_id", "project_id"],
            ["projects.tenant_id", "projects.id"],
            name="fk_migration_plans_project",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "jira_source_id"],
            ["project_jira_sources.tenant_id", "project_jira_sources.id"],
            name="fk_migration_plans_jira_source",
        ),
        sa.CheckConstraint("import_mode IN ('jira_authoritative')", name="ck_migration_plans_mode"),
        sa.CheckConstraint("status IN ('DRAFT','SCANNED','BASELINED')", name="ck_migration_plans_status"),
    )
    op.create_table(
        "migration_runs",
        sa.Column("id", UUID(as_uuid=True), primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column("tenant_id", UUID(as_uuid=True), nullable=False),
        sa.Column("project_id", UUID(as_uuid=True), nullable=False),
        sa.Column("plan_id", UUID(as_uuid=True), nullable=False),
        sa.Column("status", sa.Text, nullable=False, server_default="QUEUED"),
        sa.Column("cursor", sa.Text),
        sa.Column("pages_done", sa.Integer, nullable=False, server_default="0"),
        sa.Column("issues_seen", sa.Integer, nullable=False, server_default="0"),
        sa.Column("counts", JSONB, nullable=False, server_default=sa.text("'{}'::jsonb")),
        sa.Column("diagnostics", JSONB, nullable=False, server_default=sa.text("'{}'::jsonb")),
        sa.Column("scanned_as", JSONB, nullable=False, server_default=sa.text("'{}'::jsonb")),
        sa.Column("error", JSONB),
        _ts("created_at", nullable=False),
        _ts("started_at"),
        _ts("finished_at"),
        sa.UniqueConstraint("tenant_id", "id", name="uq_migration_runs_tenant_id"),
        sa.ForeignKeyConstraint(
            ["tenant_id", "plan_id"],
            ["migration_plans.tenant_id", "migration_plans.id"],
            name="fk_migration_runs_plan",
        ),
        sa.CheckConstraint(
            "status IN ('QUEUED','RUNNING','SUCCEEDED','FAILED')", name="ck_migration_runs_status"
        ),
    )
    op.create_index("ix_migration_runs_project", "migration_runs", ["tenant_id", "project_id", "created_at"])
    op.create_table(
        "migration_issues",
        sa.Column("tenant_id", UUID(as_uuid=True), nullable=False),
        sa.Column("project_id", UUID(as_uuid=True), nullable=False),
        sa.Column("jira_issue_id", sa.Text, nullable=False),
        sa.Column("key", sa.Text, nullable=False),
        sa.Column("original_key", sa.Text, nullable=False),
        sa.Column("type_raw", sa.Text, nullable=False),
        sa.Column("type", sa.Text, nullable=False),
        sa.Column("status_raw", sa.Text, nullable=False),
        sa.Column("status", sa.Text, nullable=False),
        sa.Column("parent_issue_id", sa.Text),
        sa.Column("parent_key", sa.Text),
        sa.Column("summary", sa.Text, nullable=False),
        sa.Column("creator", sa.Text),
        sa.Column("reporter", sa.Text),
        sa.Column("created_at", sa.Text),
        sa.Column("updated_at", sa.Text),
        sa.Column("links", JSONB, nullable=False, server_default=sa.text("'[]'::jsonb")),
        sa.Column("flags", JSONB, nullable=False, server_default=sa.text("'[]'::jsonb")),
        sa.Column("present", sa.Boolean, nullable=False, server_default=sa.text("true")),
        sa.Column("first_seen_run", UUID(as_uuid=True), nullable=False),
        sa.Column("last_seen_run", UUID(as_uuid=True), nullable=False),
        sa.PrimaryKeyConstraint("project_id", "jira_issue_id", name="pk_migration_issues"),
        sa.ForeignKeyConstraint(
            ["tenant_id", "project_id"],
            ["projects.tenant_id", "projects.id"],
            name="fk_migration_issues_project",
        ),
    )
    op.create_index("ix_migration_issues_parent", "migration_issues", ["project_id", "parent_issue_id"])
    for table, grants in TABLES.items():
        op.execute(f"GRANT {grants} ON {table} TO {APP_ROLE}")
        op.execute(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY")
        op.execute(f"ALTER TABLE {table} FORCE ROW LEVEL SECURITY")
        op.execute(
            f"CREATE POLICY tenant_isolation ON {table} "
            "USING (tenant_id = current_tenant()) WITH CHECK (tenant_id = current_tenant())"
        )
    _kinds(KINDS_NEW)


def downgrade() -> None:
    op.execute("DELETE FROM sync_jobs WHERE source_kind = 'migration'")
    op.execute("DELETE FROM source_sync_state WHERE source_kind = 'migration'")
    _kinds(KINDS_OLD)
    for table in reversed(list(TABLES)):
        op.execute(f"REVOKE ALL ON {table} FROM {APP_ROLE}")
        op.drop_table(table)
