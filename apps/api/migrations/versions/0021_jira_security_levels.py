"""Jira issue security levels (INPROOM-188 / story INPROOM-31).

Revision ID: 0021_jira_security_levels
Revises: 0020_knowledge_changes
Create Date: 2026-11-26

Each Jira source gets a `security` policy: {"include_secured": false, "allowed_levels": []}. By default an
issue under a Jira security level is withheld (RESTRICTED), mirroring Confluence's include_restricted=false.

Until now the level was recorded in the item's ACL but not enforced. This migration closes that gap at once:
stored issues whose current ACL names a security level become RESTRICTED, and the knowledge feed records each
one through its trigger (migration 0020).
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision = "0021_jira_security_levels"
down_revision = "0020_knowledge_changes"
branch_labels = None
depends_on = None

DEFAULT = '{"include_secured": false, "allowed_levels": []}'


def upgrade() -> None:
    op.add_column(
        "project_jira_sources",
        sa.Column("security", JSONB, nullable=False, server_default=sa.text(f"'{DEFAULT}'::jsonb")),
    )
    op.execute(
        "UPDATE source_objects SET status = 'RESTRICTED', "
        "status_reason = 'security level ' || (acl->>'security_level'), updated_at = now() "
        "WHERE source_kind = 'jira' AND status = 'CURRENT' AND acl->>'security_level' IS NOT NULL"
    )


def downgrade() -> None:
    op.execute(
        "UPDATE source_objects SET status = 'CURRENT', status_reason = NULL, updated_at = now() "
        "WHERE source_kind = 'jira' AND status = 'RESTRICTED' AND status_reason LIKE 'security level %'"
    )
    op.drop_column("project_jira_sources", "security")
