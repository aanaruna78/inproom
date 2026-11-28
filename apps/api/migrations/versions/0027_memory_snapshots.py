"""Markdown project memory snapshots (INPROOM-224 / story INPROOM-35).

Revision ID: 0027_memory_snapshots
Revises: 0026_revision_provider_meta
Create Date: 2026-11-28

- `memory_snapshots`: one row per version of a project's memory: the feed head it was generated at, the
  generator version, the sha256 of its Markdown body, counts per section, redactions per secret type, reason
  and actor. Unique (project, version).
- `memory_snapshot_entries`: the rendered lines in order, each naming the item (object and revision) it cites,
  and every item the line depends on (`depends_on`: a link names two endpoints), so a read withholds lines
  whose items are no longer visible and a purge deletes the lines of purged content.
Forced RLS on both. A projection: snapshots are never read back as a source.
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import ARRAY, JSONB, UUID

revision = "0027_memory_snapshots"
down_revision = "0026_revision_provider_meta"
branch_labels = None
depends_on = None

APP_ROLE = "inproom_app"
TABLES = ("memory_snapshot_entries", "memory_snapshots")


def _rls(table: str) -> None:
    op.execute(f"GRANT SELECT, INSERT, UPDATE, DELETE ON {table} TO {APP_ROLE}")
    op.execute(f"GRANT USAGE ON SEQUENCE {table}_id_seq TO {APP_ROLE}")
    op.execute(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY")
    op.execute(f"ALTER TABLE {table} FORCE ROW LEVEL SECURITY")
    op.execute(
        f"CREATE POLICY tenant_isolation ON {table} "
        "USING (tenant_id = current_tenant()) WITH CHECK (tenant_id = current_tenant())"
    )


def upgrade() -> None:
    op.create_table(
        "memory_snapshots",
        sa.Column("id", sa.BigInteger, sa.Identity(), primary_key=True),
        sa.Column("tenant_id", UUID(as_uuid=True), nullable=False),
        sa.Column("project_id", UUID(as_uuid=True), nullable=False),
        sa.Column("version", sa.Integer, nullable=False),
        sa.Column("feed_head", sa.BigInteger, nullable=False),
        sa.Column("generator", sa.Text, nullable=False),
        sa.Column("sha256", sa.Text, nullable=False),
        sa.Column("counts", JSONB, nullable=False, server_default=sa.text("'{}'::jsonb")),
        sa.Column("redactions", JSONB, nullable=False, server_default=sa.text("'{}'::jsonb")),
        sa.Column("metadata", JSONB, nullable=False, server_default=sa.text("'{}'::jsonb")),
        sa.Column("reason", sa.Text, nullable=False),
        sa.Column("created_by", sa.Text, nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.UniqueConstraint("project_id", "version", name="uq_memory_snapshots_version"),
        sa.ForeignKeyConstraint(
            ["tenant_id", "project_id"],
            ["projects.tenant_id", "projects.id"],
            name="fk_memory_snapshots_project",
        ),
    )
    op.create_table(
        "memory_snapshot_entries",
        sa.Column("id", sa.BigInteger, sa.Identity(), primary_key=True),
        sa.Column("tenant_id", UUID(as_uuid=True), nullable=False),
        sa.Column("project_id", UUID(as_uuid=True), nullable=False),
        sa.Column("snapshot_id", sa.BigInteger, nullable=False),
        sa.Column("section", sa.Text, nullable=False),
        sa.Column("ordinal", sa.Integer, nullable=False),
        sa.Column("line", sa.Text, nullable=False),
        sa.Column("citation", JSONB),
        sa.Column("object_id", UUID(as_uuid=True)),  # null: header and section lines
        sa.Column("revision_id", UUID(as_uuid=True)),
        sa.Column("depends_on", ARRAY(UUID(as_uuid=True)), nullable=False, server_default="{}"),
        sa.UniqueConstraint("snapshot_id", "ordinal", name="uq_memory_snapshot_entries_ordinal"),
        sa.ForeignKeyConstraint(
            ["snapshot_id"],
            ["memory_snapshots.id"],
            name="fk_memory_snapshot_entries_snapshot",
            ondelete="CASCADE",
        ),
    )
    op.create_index("ix_memory_snapshot_entries_object", "memory_snapshot_entries", ["object_id"])
    op.create_index(
        "ix_memory_snapshot_entries_depends_on",
        "memory_snapshot_entries",
        ["depends_on"],
        postgresql_using="gin",
    )
    for table in TABLES:
        _rls(table)


def downgrade() -> None:
    for table in TABLES:
        op.execute(f"REVOKE ALL ON {table} FROM {APP_ROLE}")
        op.drop_table(table)
