"""Revision-scoped provider metadata (INPROOM-210 / story INPROOM-30).

Revision ID: 0026_revision_provider_meta
Revises: 0025_knowledge_graph
Create Date: 2026-11-27

`source_revisions.provider_meta` keeps the provider facts that belong to ONE revision, recorded when it is
stored: a Confluence version's author and time, the commit a git revision was read at. Provenance for an old
revision reads them instead of the item's current facts. Revisions stored before this have no value: the
current one falls back to the item's current facts, older ones report the facts as not retained.
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision = "0026_revision_provider_meta"
down_revision = "0025_knowledge_graph"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("source_revisions", sa.Column("provider_meta", JSONB))


def downgrade() -> None:
    op.drop_column("source_revisions", "provider_meta")
