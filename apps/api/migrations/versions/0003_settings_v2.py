"""Settings v2 (INPROOM-102): per-version schema, change note and changed paths.

Revision ID: 0003_settings_v2
Revises: 0002_rbac
Create Date: 2026-11-23

Existing rows keep their legacy (v1) documents untouched - history is immutable - and get
schema_version = 1; the API reads them as schema v2 (app.modules.settings.upgrade).
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import ARRAY

revision = "0003_settings_v2"
down_revision = "0002_rbac"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "project_settings_versions",
        sa.Column("schema_version", sa.SmallInteger, nullable=False, server_default="1"),
    )
    op.add_column("project_settings_versions", sa.Column("change_note", sa.Text))
    op.add_column(
        "project_settings_versions",
        sa.Column("changed_paths", ARRAY(sa.Text), nullable=False, server_default=sa.text("'{}'::text[]")),
    )
    op.create_check_constraint("ck_settings_version_positive", "project_settings_versions", "version >= 1")


def downgrade() -> None:
    op.drop_constraint("ck_settings_version_positive", "project_settings_versions", type_="check")
    op.drop_column("project_settings_versions", "changed_paths")
    op.drop_column("project_settings_versions", "change_note")
    op.drop_column("project_settings_versions", "schema_version")
