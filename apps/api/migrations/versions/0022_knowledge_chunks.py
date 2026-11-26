"""Retrieval chunks (INPROOM-203 / story INPROOM-32).

Revision ID: 0022_knowledge_chunks
Revises: 0021_jira_security_levels
Create Date: 2026-11-26

`knowledge_chunks` is the queryable projection of READY `chunks@1` derivatives: one row per chunk, keyed to
the derivative, revision and object, carrying its citation (kind, source, external id, seq, anchor), context,
text, token estimate and sha256. Rows are written in the transaction that marks the derivative READY and are
dropped when it leaves READY (superseded, invalidated, purged), like code_symbols. Forced RLS.
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import ARRAY, UUID

revision = "0022_knowledge_chunks"
down_revision = "0021_jira_security_levels"
branch_labels = None
depends_on = None

APP_ROLE = "inproom_app"


def upgrade() -> None:
    op.create_table(
        "knowledge_chunks",
        sa.Column("id", sa.BigInteger, sa.Identity(), primary_key=True),
        sa.Column("tenant_id", UUID(as_uuid=True), nullable=False),
        sa.Column("project_id", UUID(as_uuid=True), nullable=False),
        sa.Column("derivative_id", UUID(as_uuid=True), nullable=False),
        sa.Column("revision_id", UUID(as_uuid=True), nullable=False),
        sa.Column("object_id", UUID(as_uuid=True), nullable=False),
        sa.Column("source_kind", sa.Text, nullable=False),
        sa.Column("source_id", UUID(as_uuid=True), nullable=False),
        sa.Column("external_id", sa.Text, nullable=False),
        sa.Column("seq", sa.Integer, nullable=False),
        sa.Column("ordinal", sa.Integer, nullable=False),
        sa.Column("anchor", sa.Text),
        sa.Column("path", ARRAY(sa.Text), nullable=False, server_default="{}"),
        sa.Column("page", sa.Integer),
        sa.Column("line_start", sa.Integer),
        sa.Column("line_end", sa.Integer),
        sa.Column("context", sa.Text, nullable=False, server_default=""),
        sa.Column("text", sa.Text, nullable=False),
        sa.Column("tokens", sa.Integer, nullable=False),
        sa.Column("sha256", sa.Text, nullable=False),
        sa.UniqueConstraint("derivative_id", "ordinal", name="uq_knowledge_chunks_ordinal"),
        sa.ForeignKeyConstraint(
            ["tenant_id", "project_id"],
            ["projects.tenant_id", "projects.id"],
            name="fk_knowledge_chunks_project",
        ),
    )
    op.create_index("ix_knowledge_chunks_revision", "knowledge_chunks", ["project_id", "revision_id"])
    op.create_index("ix_knowledge_chunks_sha", "knowledge_chunks", ["tenant_id", "sha256"])
    op.execute(f"GRANT SELECT, INSERT, UPDATE, DELETE ON knowledge_chunks TO {APP_ROLE}")
    op.execute(f"GRANT USAGE ON SEQUENCE knowledge_chunks_id_seq TO {APP_ROLE}")
    op.execute("ALTER TABLE knowledge_chunks ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE knowledge_chunks FORCE ROW LEVEL SECURITY")
    op.execute(
        "CREATE POLICY tenant_isolation ON knowledge_chunks "
        "USING (tenant_id = current_tenant()) WITH CHECK (tenant_id = current_tenant())"
    )


def downgrade() -> None:
    op.execute(f"REVOKE ALL ON knowledge_chunks FROM {APP_ROLE}")
    op.drop_table("knowledge_chunks")
