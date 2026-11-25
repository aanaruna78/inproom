"""Per-item verified commit; retired branch snapshots (INPROOM-177/179 / story INPROOM-29).

Revision ID: 0018_code_verified_ref
Revises: 0017_code_symbol_commit
Create Date: 2026-11-26

- source_objects.verified_ref: the newest provider ref (for git: commit) at which the item's CURRENT content
  was confirmed. The code job sets it for every file it records or finds unchanged, so a citation always
  names a commit that contains exactly the cited content. Backfilled from the commit each git revision
  recorded.
- code_snapshots.status gains RETIRED: snapshots of a branch that is no longer selected.
"""

import sqlalchemy as sa
from alembic import op

revision = "0018_code_verified_ref"
down_revision = "0017_code_symbol_commit"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("source_objects", sa.Column("verified_ref", sa.Text))
    op.execute(
        "UPDATE source_objects o SET verified_ref = r.acl->>'commit' FROM source_revisions r "
        "WHERE r.id = o.current_revision_id AND o.source_kind = 'git' AND r.acl ? 'commit'"
    )
    op.drop_constraint("ck_code_snapshots_status", "code_snapshots", type_="check")
    op.create_check_constraint(
        "ck_code_snapshots_status", "code_snapshots", "status IN ('RUNNING','COMPLETE','PARTIAL','RETIRED')"
    )


def downgrade() -> None:
    op.execute("UPDATE code_snapshots SET status = 'COMPLETE' WHERE status = 'RETIRED'")
    op.drop_constraint("ck_code_snapshots_status", "code_snapshots", type_="check")
    op.create_check_constraint(
        "ck_code_snapshots_status", "code_snapshots", "status IN ('RUNNING','COMPLETE','PARTIAL')"
    )
    op.drop_column("source_objects", "verified_ref")
