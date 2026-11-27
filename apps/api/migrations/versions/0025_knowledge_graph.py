"""Knowledge graph (INPROOM-215 / story INPROOM-34).

Revision ID: 0025_knowledge_graph
Revises: 0024_knowledge_chunks_tsv
Create Date: 2026-11-27

The graph is a projection of the content store, rebuilt per item by the `graph` job:
- `graph_nodes`: node *occurrences* - one row per (natural key, item that asserts it). An item's own node has
  role `item`; entities it names (a Jira component, a version, a code owner, a commit) have role `derived`.
  The same key from several items is one node with several occurrences (duplicates collapse by key).
- `graph_edges`: edges between natural keys, each produced by one item's extraction (`object_id`), with origin
  (validated: stated by a source field; inferred: read from text), confidence, extractor and evidence.
- `graph_sources`: which revision of each item the graph was extracted from, by which extractor.
- `graph_reviews`: curators' decisions (confirm, reject, manual edge), keyed by (from_key, kind, to_key) so
  that they survive every rebuild. The only durable user data here.
The sync queue accepts `graph` jobs. Forced RLS on all four tables.
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB, UUID

revision = "0025_knowledge_graph"
down_revision = "0024_knowledge_chunks_tsv"
branch_labels = None
depends_on = None

APP_ROLE = "inproom_app"
KINDS_OLD = "'git','jira','confluence','connection','migration','derive','purge','sweep','code','embed'"
KINDS_NEW = (
    "'git','jira','confluence','connection','migration','derive','purge','sweep','code','embed','graph'"
)
TABLES = ("graph_reviews", "graph_sources", "graph_edges", "graph_nodes")


def _kinds(kinds: str) -> None:
    for table, name in (
        ("sync_jobs", "ck_sync_jobs_kind"),
        ("source_sync_state", "ck_source_sync_state_kind"),
    ):
        op.drop_constraint(name, table, type_="check")
        op.create_check_constraint(name, table, f"source_kind IN ({kinds})")


def _project_fk(table: str) -> sa.ForeignKeyConstraint:
    return sa.ForeignKeyConstraint(
        ["tenant_id", "project_id"], ["projects.tenant_id", "projects.id"], name=f"fk_{table}_project"
    )


def _rls(table: str) -> None:
    op.execute(f"GRANT SELECT, INSERT, UPDATE, DELETE ON {table} TO {APP_ROLE}")
    op.execute(f"GRANT USAGE ON SEQUENCE {table}_id_seq TO {APP_ROLE}")
    op.execute(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY")
    op.execute(f"ALTER TABLE {table} FORCE ROW LEVEL SECURITY")
    op.execute(
        f"CREATE POLICY tenant_isolation ON {table} "
        "USING (tenant_id = current_tenant()) WITH CHECK (tenant_id = current_tenant())"
    )


def _base(table: str, *columns) -> None:
    op.create_table(
        table,
        sa.Column("id", sa.BigInteger, sa.Identity(), primary_key=True),
        sa.Column("tenant_id", UUID(as_uuid=True), nullable=False),
        sa.Column("project_id", UUID(as_uuid=True), nullable=False),
        *columns,
        _project_fk(table),
    )


def upgrade() -> None:
    now = sa.func.now()
    _base(
        "graph_nodes",
        sa.Column("key", sa.Text, nullable=False),
        sa.Column("type", sa.Text, nullable=False),
        sa.Column("label", sa.Text, nullable=False, server_default=""),
        sa.Column("role", sa.Text, nullable=False),
        sa.Column("attributes", JSONB, nullable=False, server_default=sa.text("'{}'::jsonb")),
        sa.Column("object_id", UUID(as_uuid=True), nullable=False),
        sa.Column("revision_id", UUID(as_uuid=True), nullable=False),
        sa.UniqueConstraint("project_id", "key", "object_id", name="uq_graph_nodes_occurrence"),
        sa.CheckConstraint("role IN ('item','derived')", name="ck_graph_nodes_role"),
    )
    op.create_index("ix_graph_nodes_object", "graph_nodes", ["object_id"])
    _base(
        "graph_edges",
        sa.Column("from_key", sa.Text, nullable=False),
        sa.Column("kind", sa.Text, nullable=False),
        sa.Column("to_key", sa.Text, nullable=False),
        sa.Column("origin", sa.Text, nullable=False),
        sa.Column("confidence", sa.Float, nullable=False),
        sa.Column("extractor", sa.Text, nullable=False),
        sa.Column("evidence", JSONB, nullable=False, server_default=sa.text("'[]'::jsonb")),
        sa.Column("object_id", UUID(as_uuid=True), nullable=False),
        sa.Column("revision_id", UUID(as_uuid=True), nullable=False),
        sa.UniqueConstraint(
            "project_id", "object_id", "from_key", "kind", "to_key", name="uq_graph_edges_key"
        ),
        sa.CheckConstraint("origin IN ('validated','inferred')", name="ck_graph_edges_origin"),
    )
    op.create_index("ix_graph_edges_from", "graph_edges", ["project_id", "from_key"])
    op.create_index("ix_graph_edges_to", "graph_edges", ["project_id", "to_key"])
    op.create_index("ix_graph_edges_object", "graph_edges", ["object_id"])
    _base(
        "graph_sources",
        sa.Column("object_id", UUID(as_uuid=True), nullable=False),
        sa.Column("revision_id", UUID(as_uuid=True), nullable=False),
        sa.Column("extractor", sa.Text, nullable=False),
        sa.Column("extracted_at", sa.DateTime(timezone=True), nullable=False, server_default=now),
        sa.UniqueConstraint("object_id", name="uq_graph_sources_object"),
    )
    _base(
        "graph_reviews",
        sa.Column("from_key", sa.Text, nullable=False),
        sa.Column("kind", sa.Text, nullable=False),
        sa.Column("to_key", sa.Text, nullable=False),
        sa.Column("action", sa.Text, nullable=False),
        sa.Column("reason", sa.Text, nullable=False),
        sa.Column("actor", sa.Text, nullable=False),
        sa.Column("at", sa.DateTime(timezone=True), nullable=False, server_default=now),
        sa.UniqueConstraint("project_id", "from_key", "kind", "to_key", name="uq_graph_reviews_edge"),
        sa.CheckConstraint("action IN ('confirm','reject','manual')", name="ck_graph_reviews_action"),
    )
    for table in TABLES:
        _rls(table)
    _kinds(KINDS_NEW)


def downgrade() -> None:
    op.execute("DELETE FROM sync_jobs WHERE source_kind = 'graph'")
    op.execute("DELETE FROM source_sync_state WHERE source_kind = 'graph'")
    _kinds(KINDS_OLD)
    for table in TABLES:
        op.execute(f"REVOKE ALL ON {table} FROM {APP_ROLE}")
        op.drop_table(table)
