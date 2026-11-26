"""Source trust and classification policies (INPROOM-183 / story INPROOM-30).

Revision ID: 0019_source_policies
Revises: 0018_code_verified_ref
Create Date: 2026-11-26

One row per project source that has an explicit policy (sources without one use the defaults by kind):
trust tier and classification, with the reason, who set it, and a version for If-Match. Forced RLS.
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import UUID

revision = "0019_source_policies"
down_revision = "0018_code_verified_ref"
branch_labels = None
depends_on = None

APP_ROLE = "inproom_app"


def upgrade() -> None:
    op.create_table(
        "source_policies",
        sa.Column("id", UUID(as_uuid=True), primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column("tenant_id", UUID(as_uuid=True), nullable=False),
        sa.Column("project_id", UUID(as_uuid=True), nullable=False),
        sa.Column("source_kind", sa.Text, nullable=False),
        sa.Column("source_id", UUID(as_uuid=True), nullable=False),
        sa.Column("trust", sa.Text, nullable=False),
        sa.Column("classification", sa.Text, nullable=False),
        sa.Column("reason", sa.Text, nullable=False),
        sa.Column("updated_by", sa.Text, nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("version", sa.Integer, nullable=False, server_default="1"),
        sa.UniqueConstraint("source_kind", "source_id", name="uq_source_policies_source"),
        sa.ForeignKeyConstraint(
            ["tenant_id", "project_id"],
            ["projects.tenant_id", "projects.id"],
            name="fk_source_policies_project",
        ),
        sa.CheckConstraint(
            "source_kind IN ('confluence','jira','git','upload')", name="ck_source_policies_kind"
        ),
        sa.CheckConstraint(
            "trust IN ('authoritative','trusted','reference','untrusted')", name="ck_source_policies_trust"
        ),
        sa.CheckConstraint(
            "classification IN ('public','internal','confidential')", name="ck_source_policies_classification"
        ),
    )
    op.execute(f"GRANT SELECT, INSERT, UPDATE, DELETE ON source_policies TO {APP_ROLE}")
    op.execute("ALTER TABLE source_policies ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE source_policies FORCE ROW LEVEL SECURITY")
    op.execute(
        "CREATE POLICY tenant_isolation ON source_policies "
        "USING (tenant_id = current_tenant()) WITH CHECK (tenant_id = current_tenant())"
    )


def downgrade() -> None:
    op.execute(f"REVOKE ALL ON source_policies FROM {APP_ROLE}")
    op.drop_table("source_policies")
