"""Bind each migration scan run to the plan revision it scanned (INPROOM-155 / story INPROOM-19).

Revision ID: 0011_migration_run_plan_version
Revises: 0010_migration_baseline
Create Date: 2026-11-25

A baseline may only be confirmed from a completed scan of the CURRENT plan revision (same Jira source and
history scope). Existing runs are backfilled with their plan's version only if the plan has not moved since;
that is conservative: an older run then no longer qualifies and a re-scan is needed.
"""

import sqlalchemy as sa
from alembic import op

revision = "0011_migration_run_plan_version"
down_revision = "0010_migration_baseline"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("migration_runs", sa.Column("plan_version", sa.Integer, nullable=False, server_default="0"))
    op.execute(
        "UPDATE migration_runs r SET plan_version = p.version FROM migration_plans p "
        "WHERE p.id = r.plan_id AND r.created_at >= p.updated_at"
    )


def downgrade() -> None:
    op.drop_column("migration_runs", "plan_version")
