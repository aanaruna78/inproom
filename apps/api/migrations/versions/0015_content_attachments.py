"""Attachments linked to their page; index for parse-failure listings (INPROOM-166 / story INPROOM-28).

Revision ID: 0015_content_attachments
Revises: 0014_content_blob_sweep
Create Date: 2026-11-26

- source_objects.parent_external_id: the item an attachment belongs to (its Confluence page id); NULL for
  top-level items. Attachments follow their page's scope and ACL.
- ix_derivatives_producer_status: (tenant, project, producer, status) for "which parses failed" (INPROOM-167).
"""

import sqlalchemy as sa
from alembic import op

revision = "0015_content_attachments"
down_revision = "0014_content_blob_sweep"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("source_objects", sa.Column("parent_external_id", sa.Text))
    op.create_index(
        "ix_source_objects_parent",
        "source_objects",
        ["project_id", "source_kind", "source_id", "parent_external_id"],
        postgresql_where=sa.text("parent_external_id IS NOT NULL"),
    )
    op.create_index(
        "ix_derivatives_producer_status", "derivatives", ["tenant_id", "project_id", "producer", "status"]
    )


def downgrade() -> None:
    op.drop_index("ix_derivatives_producer_status", table_name="derivatives")
    op.drop_index("ix_source_objects_parent", table_name="source_objects")
    op.drop_column("source_objects", "parent_external_id")
