"""Repository code index: per-branch snapshots and the symbol projection (INPROOM-173 / story INPROOM-29).

Revision ID: 0016_code_index
Revises: 0015_content_attachments
Create Date: 2026-11-26

- code_snapshots: one row per (repository source, branch, commit) the code job indexed - complete or partial,
  counts, skipped paths with reasons, renames, and the commit's CODEOWNERS text.
- code_symbols: definitions projected from READY symbols derivatives (filled by INPROOM-175), for search.
- sync_jobs / source_sync_state accept kind 'code' (source_id = repository source).
Both tables are tenant-scoped with FORCE ROW LEVEL SECURITY.
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB, UUID

revision = "0016_code_index"
down_revision = "0015_content_attachments"
branch_labels = None
depends_on = None

APP_ROLE = "inproom_app"
TABLES = {
    "code_snapshots": "SELECT, INSERT, UPDATE, DELETE",
    "code_symbols": "SELECT, INSERT, UPDATE, DELETE",
}
KINDS_OLD = "'git','jira','confluence','connection','migration','derive','purge','sweep'"
KINDS_NEW = "'git','jira','confluence','connection','migration','derive','purge','sweep','code'"


def _kinds(kinds: str) -> None:
    for table, name in (
        ("sync_jobs", "ck_sync_jobs_kind"),
        ("source_sync_state", "ck_source_sync_state_kind"),
    ):
        op.drop_constraint(name, table, type_="check")
        op.create_check_constraint(name, table, f"source_kind IN ({kinds})")


def upgrade() -> None:
    op.create_table(
        "code_snapshots",
        sa.Column("id", UUID(as_uuid=True), primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column("tenant_id", UUID(as_uuid=True), nullable=False),
        sa.Column("project_id", UUID(as_uuid=True), nullable=False),
        sa.Column("source_id", UUID(as_uuid=True), nullable=False),
        sa.Column("branch", sa.Text, nullable=False),
        sa.Column("commit_sha", sa.Text, nullable=False),
        sa.Column("status", sa.Text, nullable=False, server_default="RUNNING"),
        sa.Column("truncated", sa.Boolean, nullable=False, server_default=sa.text("false")),
        sa.Column("counts", JSONB, nullable=False, server_default=sa.text("'{}'::jsonb")),
        sa.Column("skipped", JSONB, nullable=False, server_default=sa.text("'[]'::jsonb")),
        sa.Column("renames", JSONB, nullable=False, server_default=sa.text("'[]'::jsonb")),
        sa.Column("skipped_blobs", JSONB, nullable=False, server_default=sa.text("'{}'::jsonb")),
        sa.Column("codeowners", JSONB),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("finished_at", sa.DateTime(timezone=True)),
        sa.UniqueConstraint("source_id", "branch", "commit_sha", name="uq_code_snapshots_commit"),
        sa.UniqueConstraint("tenant_id", "id", name="uq_code_snapshots_tenant_id"),
        sa.ForeignKeyConstraint(
            ["tenant_id", "project_id"],
            ["projects.tenant_id", "projects.id"],
            name="fk_code_snapshots_project",
        ),
        sa.CheckConstraint("status IN ('RUNNING','COMPLETE','PARTIAL')", name="ck_code_snapshots_status"),
    )
    op.create_index(
        "ix_code_snapshots_branch", "code_snapshots", ["tenant_id", "source_id", "branch", "started_at"]
    )
    op.create_table(
        "code_symbols",
        sa.Column("id", sa.BigInteger, sa.Identity(), primary_key=True),
        sa.Column("tenant_id", UUID(as_uuid=True), nullable=False),
        sa.Column("project_id", UUID(as_uuid=True), nullable=False),
        sa.Column("derivative_id", UUID(as_uuid=True), nullable=False),
        sa.Column("revision_id", UUID(as_uuid=True), nullable=False),
        sa.Column("object_id", UUID(as_uuid=True), nullable=False),
        sa.Column("source_id", UUID(as_uuid=True), nullable=False),
        sa.Column("branch", sa.Text, nullable=False),
        sa.Column("path", sa.Text, nullable=False),
        sa.Column("blob_sha", sa.Text, nullable=False),
        sa.Column("language", sa.Text, nullable=False),
        sa.Column("kind", sa.Text, nullable=False),
        sa.Column("name", sa.Text, nullable=False),
        sa.Column("container", sa.Text),
        sa.Column("signature", sa.Text),
        sa.Column("line_start", sa.Integer, nullable=False),
        sa.Column("line_end", sa.Integer, nullable=False),
        sa.ForeignKeyConstraint(
            ["tenant_id", "project_id"], ["projects.tenant_id", "projects.id"], name="fk_code_symbols_project"
        ),
    )
    op.create_index(
        "ix_code_symbols_name", "code_symbols", ["tenant_id", "project_id", sa.text("lower(name)")]
    )
    op.create_index("ix_code_symbols_derivative", "code_symbols", ["derivative_id"])
    for table, grants in TABLES.items():
        op.execute(f"GRANT {grants} ON {table} TO {APP_ROLE}")
        op.execute(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY")
        op.execute(f"ALTER TABLE {table} FORCE ROW LEVEL SECURITY")
        op.execute(
            f"CREATE POLICY tenant_isolation ON {table} "
            "USING (tenant_id = current_tenant()) WITH CHECK (tenant_id = current_tenant())"
        )
    op.execute(f"GRANT USAGE ON SEQUENCE code_symbols_id_seq TO {APP_ROLE}")
    _kinds(KINDS_NEW)


def downgrade() -> None:
    op.execute("DELETE FROM sync_jobs WHERE source_kind = 'code'")
    op.execute("DELETE FROM source_sync_state WHERE source_kind = 'code'")
    _kinds(KINDS_OLD)
    for table in reversed(list(TABLES)):
        op.execute(f"REVOKE ALL ON {table} FROM {APP_ROLE}")
        op.drop_table(table)
