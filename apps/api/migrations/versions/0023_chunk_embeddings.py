"""Chunk embeddings (INPROOM-204 / story INPROOM-32).

Revision ID: 0023_chunk_embeddings
Revises: 0022_knowledge_chunks
Create Date: 2026-11-26

- `chunk_embeddings`: one pgvector per (tenant, model, chunk sha256). Identical chunks of a tenant are
  embedded once; vectors are never shared across tenants. The column is an untyped `vector`, because
  dimensions vary by model. Each model gets its own partial HNSW cosine index on `embedding::vector(n)`
  (`app.content.embed.ensure_index`); the default local model's index is created here.
- `chunk_embedding_failures`: chunks the provider permanently rejected for a model, with the reason and
  attempts (the failures list), cleared when the chunk is embedded.
- The sync queue accepts `embed` jobs.
Forced RLS on both tables.
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import UUID

revision = "0023_chunk_embeddings"
down_revision = "0022_knowledge_chunks"
branch_labels = None
depends_on = None

APP_ROLE = "inproom_app"
KINDS_OLD = "'git','jira','confluence','connection','migration','derive','purge','sweep','code'"
KINDS_NEW = "'git','jira','confluence','connection','migration','derive','purge','sweep','code','embed'"
LOCAL_INDEX = (
    "CREATE INDEX IF NOT EXISTS ix_chunk_embeddings_local_hash_v1 ON chunk_embeddings "
    "USING hnsw ((embedding::vector(256)) vector_cosine_ops) WHERE model = 'local-hash-v1'"
)


def _kinds(kinds: str) -> None:
    for table, name in (
        ("sync_jobs", "ck_sync_jobs_kind"),
        ("source_sync_state", "ck_source_sync_state_kind"),
    ):
        op.drop_constraint(name, table, type_="check")
        op.create_check_constraint(name, table, f"source_kind IN ({kinds})")


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
    op.execute("CREATE EXTENSION IF NOT EXISTS vector")
    op.create_table(
        "chunk_embeddings",
        sa.Column("id", sa.BigInteger, sa.Identity(), primary_key=True),
        sa.Column("tenant_id", UUID(as_uuid=True), nullable=False),
        sa.Column("model", sa.Text, nullable=False),
        sa.Column("model_version", sa.Text, nullable=False),
        sa.Column("dimensions", sa.Integer, nullable=False),
        sa.Column("sha256", sa.Text, nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.UniqueConstraint("tenant_id", "model", "sha256", name="uq_chunk_embeddings_key"),
    )
    op.execute("ALTER TABLE chunk_embeddings ADD COLUMN embedding vector NOT NULL")
    op.create_table(
        "chunk_embedding_failures",
        sa.Column("id", sa.BigInteger, sa.Identity(), primary_key=True),
        sa.Column("tenant_id", UUID(as_uuid=True), nullable=False),
        sa.Column("project_id", UUID(as_uuid=True), nullable=False),
        sa.Column("model", sa.Text, nullable=False),
        sa.Column("sha256", sa.Text, nullable=False),
        sa.Column("code", sa.Text, nullable=False),
        sa.Column("detail", sa.Text, nullable=False),
        sa.Column("attempts", sa.Integer, nullable=False, server_default="1"),
        sa.Column("last_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.UniqueConstraint("project_id", "model", "sha256", name="uq_chunk_embedding_failures_key"),
        sa.ForeignKeyConstraint(
            ["tenant_id", "project_id"],
            ["projects.tenant_id", "projects.id"],
            name="fk_chunk_embedding_failures_project",
        ),
    )
    _rls("chunk_embeddings")
    _rls("chunk_embedding_failures")
    op.execute(LOCAL_INDEX)
    _kinds(KINDS_NEW)


def downgrade() -> None:
    op.execute("DELETE FROM sync_jobs WHERE source_kind = 'embed'")
    op.execute("DELETE FROM source_sync_state WHERE source_kind = 'embed'")
    _kinds(KINDS_OLD)
    for table in ("chunk_embedding_failures", "chunk_embeddings"):
        op.execute(f"REVOKE ALL ON {table} FROM {APP_ROLE}")
        op.drop_table(table)
