"""Content store: source objects, their revision chains and derivatives (INPROOM-157 / story INPROOM-27).

Revision ID: 0012_content_store
Revises: 0011_migration_run_plan_version
Create Date: 2026-11-25

Original bytes live in the object store under a content address; these tables hold identity, lineage, ACL
snapshots and the derivative registry. All three are tenant-scoped with FORCE ROW LEVEL SECURITY.
sync_jobs / source_sync_state accept the kinds 'derive' (source_id = a project source) and 'purge'
(source_id = a project).
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB, UUID

revision = "0012_content_store"
down_revision = "0011_migration_run_plan_version"
branch_labels = None
depends_on = None

APP_ROLE = "inproom_app"
TABLES = {
    "source_objects": "SELECT, INSERT, UPDATE, DELETE",
    "source_revisions": "SELECT, INSERT, UPDATE, DELETE",
    "derivatives": "SELECT, INSERT, UPDATE, DELETE",
}
KINDS_OLD = "'git','jira','confluence','connection','migration'"
KINDS_NEW = "'git','jira','confluence','connection','migration','derive','purge'"


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
        "source_objects",
        sa.Column("id", UUID(as_uuid=True), primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column("tenant_id", UUID(as_uuid=True), nullable=False),
        sa.Column("project_id", UUID(as_uuid=True), nullable=False),
        sa.Column("source_kind", sa.Text, nullable=False),
        sa.Column("source_id", UUID(as_uuid=True), nullable=False),
        sa.Column("external_id", sa.Text, nullable=False),
        sa.Column("title", sa.Text, nullable=False, server_default=""),
        sa.Column("uri", sa.Text),
        sa.Column("status", sa.Text, nullable=False, server_default="CURRENT"),
        sa.Column("status_reason", sa.Text),
        sa.Column("acl", JSONB, nullable=False, server_default=sa.text("'{}'::jsonb")),
        sa.Column("current_revision_id", UUID(as_uuid=True)),
        sa.Column("revisions", sa.Integer, nullable=False, server_default="0"),
        _ts("first_seen_at", nullable=False),
        _ts("last_seen_at", nullable=False),
        _ts("updated_at", nullable=False),
        _ts("deleted_at"),
        sa.UniqueConstraint(
            "project_id", "source_kind", "source_id", "external_id", name="uq_source_objects_identity"
        ),
        sa.UniqueConstraint("tenant_id", "id", name="uq_source_objects_tenant_id"),
        sa.ForeignKeyConstraint(
            ["tenant_id", "project_id"],
            ["projects.tenant_id", "projects.id"],
            name="fk_source_objects_project",
        ),
        sa.CheckConstraint(
            "source_kind IN ('confluence','jira','git','upload')", name="ck_source_objects_kind"
        ),
        sa.CheckConstraint(
            "status IN ('CURRENT','RESTRICTED','EXCLUDED','DELETED','TOO_LARGE')",
            name="ck_source_objects_status",
        ),
    )
    op.create_index(
        "ix_source_objects_source", "source_objects", ["tenant_id", "project_id", "source_kind", "source_id"]
    )
    op.create_table(
        "source_revisions",
        sa.Column("id", UUID(as_uuid=True), primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column("tenant_id", UUID(as_uuid=True), nullable=False),
        sa.Column("object_id", UUID(as_uuid=True), nullable=False),
        sa.Column("seq", sa.Integer, nullable=False),
        sa.Column("parent_revision_id", UUID(as_uuid=True)),
        sa.Column("provider_version", sa.Text),
        sa.Column("content_sha256", sa.Text, nullable=False),
        sa.Column("size_bytes", sa.BigInteger, nullable=False),
        sa.Column("media_type", sa.Text, nullable=False),
        sa.Column("blob_key", sa.Text),
        sa.Column("acl", JSONB, nullable=False, server_default=sa.text("'{}'::jsonb")),
        sa.Column("job_id", UUID(as_uuid=True)),
        _ts("fetched_at", nullable=False),
        _ts("purged_at"),
        sa.UniqueConstraint("object_id", "seq", name="uq_source_revisions_seq"),
        sa.UniqueConstraint("tenant_id", "id", name="uq_source_revisions_tenant_id"),
        sa.ForeignKeyConstraint(
            ["tenant_id", "object_id"],
            ["source_objects.tenant_id", "source_objects.id"],
            name="fk_source_revisions_object",
            ondelete="CASCADE",
        ),
        sa.CheckConstraint("seq >= 1", name="ck_source_revisions_seq"),
    )
    op.create_index("ix_source_revisions_sha", "source_revisions", ["tenant_id", "content_sha256"])
    op.create_table(
        "derivatives",
        sa.Column("id", UUID(as_uuid=True), primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column("tenant_id", UUID(as_uuid=True), nullable=False),
        sa.Column("project_id", UUID(as_uuid=True), nullable=False),
        sa.Column("revision_id", UUID(as_uuid=True), nullable=False),
        sa.Column("kind", sa.Text, nullable=False),
        sa.Column("producer", sa.Text, nullable=False),
        sa.Column("producer_version", sa.Integer, nullable=False),
        sa.Column("status", sa.Text, nullable=False, server_default="PENDING"),
        sa.Column("output_sha256", sa.Text),
        sa.Column("size_bytes", sa.BigInteger),
        sa.Column("blob_key", sa.Text),
        sa.Column("error", JSONB),
        sa.Column("reason", sa.Text),
        _ts("created_at", nullable=False),
        _ts("built_at"),
        _ts("invalidated_at"),
        _ts("purged_at"),
        sa.ForeignKeyConstraint(
            ["tenant_id", "revision_id"],
            ["source_revisions.tenant_id", "source_revisions.id"],
            name="fk_derivatives_revision",
            ondelete="CASCADE",
        ),
        sa.CheckConstraint(
            "status IN ('PENDING','READY','FAILED','SUPERSEDED','INVALIDATED')", name="ck_derivatives_status"
        ),
    )
    op.create_index(
        "uq_derivatives_live",
        "derivatives",
        ["revision_id", "kind", "producer", "producer_version"],
        unique=True,
        postgresql_where=sa.text("status IN ('PENDING','READY','FAILED')"),
    )
    op.create_index("ix_derivatives_project_status", "derivatives", ["tenant_id", "project_id", "status"])
    op.create_index("ix_derivatives_sha", "derivatives", ["tenant_id", "output_sha256"])
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
    op.execute("DELETE FROM sync_jobs WHERE source_kind IN ('derive','purge')")
    op.execute("DELETE FROM source_sync_state WHERE source_kind IN ('derive','purge')")
    _kinds(KINDS_OLD)
    for table in reversed(list(TABLES)):
        op.execute(f"REVOKE ALL ON {table} FROM {APP_ROLE}")
        op.drop_table(table)
