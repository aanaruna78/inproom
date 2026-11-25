"""Per-issue baseline snapshot for migration drift (INPROOM-152 / story INPROOM-19).

Revision ID: 0010_migration_baseline
Revises: 0009_migration
Create Date: 2026-11-25

Confirming a migration baseline copies each present issue's comparable fields into baseline_snapshot, so later
re-scans can report drift (added / changed / removed) without ever moving the confirmed baseline.
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision = "0010_migration_baseline"
down_revision = "0009_migration"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("migration_issues", sa.Column("baseline_snapshot", JSONB))


def downgrade() -> None:
    op.drop_column("migration_issues", "baseline_snapshot")
