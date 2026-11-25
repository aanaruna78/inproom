"""Commit at which a projected symbol's content was recorded (INPROOM-175 / story INPROOM-29).

Revision ID: 0017_code_symbol_commit
Revises: 0016_code_index
Create Date: 2026-11-26

Citations name a commit. When the branch's latest snapshot is COMPLETE every indexed file is verified at that
commit; when it is PARTIAL a hit cites the commit at which its own content was recorded - this column.
"""

import sqlalchemy as sa
from alembic import op

revision = "0017_code_symbol_commit"
down_revision = "0016_code_index"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("code_symbols", sa.Column("commit_sha", sa.Text, nullable=False, server_default=""))


def downgrade() -> None:
    op.drop_column("code_symbols", "commit_sha")
