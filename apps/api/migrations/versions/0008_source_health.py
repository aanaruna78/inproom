"""Source health: explicit pause causes, user exclusion, scheduled connection health (INPROOM-145).

Revision ID: 0008_source_health
Revises: 0007_sync_jobs
Create Date: 2026-11-24

- The three source tables get pause_cause ('access' | 'connection_revoked' | 'excluded') plus excluded_by/_at,
  so recovery logic never has to parse status_reason text: automatic checks resume only 'access' pauses.
- sync_jobs / source_sync_state accept kind 'connection' (project-less health-check jobs).
- enqueue_due_health_checks(interval, tenant): SECURITY DEFINER, like the INPROOM-25 queue functions.
"""

import sqlalchemy as sa
from alembic import op

revision = "0008_source_health"
down_revision = "0007_sync_jobs"
branch_labels = None
depends_on = None

APP_ROLE = "inproom_app"
SOURCE_TABLES = ("project_repositories", "project_jira_sources", "project_confluence_sources")
CAUSES = "('access','connection_revoked','excluded')"

FUNCTION = """
CREATE FUNCTION enqueue_due_health_checks(p_interval_seconds integer, p_tenant uuid DEFAULT NULL)
RETURNS integer
LANGUAGE plpgsql SECURITY DEFINER SET search_path = public, pg_temp AS $$
DECLARE
    queued integer := 0;
BEGIN
    WITH due AS (
        SELECT c.id, c.tenant_id
          FROM source_connections c
          LEFT JOIN source_sync_state st ON st.source_kind = 'connection' AND st.source_id = c.id
         WHERE c.status IN ('ACTIVE', 'DEGRADED', 'INVALID')
           AND (st.next_due_at IS NULL OR st.next_due_at <= now())
           AND (p_tenant IS NULL OR c.tenant_id = p_tenant)
    ), inserted AS (
        INSERT INTO sync_jobs (tenant_id, project_id, source_kind, source_id, reason, mode)
        SELECT tenant_id, NULL, 'connection', id, 'schedule', 'incremental' FROM due
        ON CONFLICT (source_kind, source_id) WHERE status = 'QUEUED' DO NOTHING
        RETURNING 1
    ), scheduled AS (
        INSERT INTO source_sync_state (tenant_id, project_id, source_kind, source_id, next_due_at)
        SELECT tenant_id, NULL, 'connection', id, now() + make_interval(secs => p_interval_seconds) FROM due
        ON CONFLICT (source_kind, source_id) DO UPDATE SET next_due_at = EXCLUDED.next_due_at
        RETURNING 1
    )
    SELECT count(*) INTO queued FROM inserted;
    RETURN queued;
END;
$$;
"""


def _kinds(table: str, name: str, kinds: str) -> None:
    op.drop_constraint(name, table, type_="check")
    op.create_check_constraint(name, table, f"source_kind IN ({kinds})")


def upgrade() -> None:
    for table in SOURCE_TABLES:
        op.add_column(table, sa.Column("pause_cause", sa.Text))
        op.add_column(table, sa.Column("excluded_by", sa.Text))
        op.add_column(table, sa.Column("excluded_at", sa.DateTime(timezone=True)))
        op.create_check_constraint(f"ck_{table}_pause_cause", table, f"pause_cause IN {CAUSES}")
        op.execute(
            f"UPDATE {table} SET pause_cause = CASE WHEN status_reason = 'connection revoked' "
            "THEN 'connection_revoked' ELSE 'access' END WHERE status = 'PAUSED'"
        )
    _kinds("sync_jobs", "ck_sync_jobs_kind", "'git','jira','confluence','connection'")
    _kinds("source_sync_state", "ck_source_sync_state_kind", "'git','jira','confluence','connection'")
    op.alter_column("sync_jobs", "project_id", nullable=True)
    op.alter_column("source_sync_state", "project_id", nullable=True)
    op.execute(FUNCTION)
    op.execute("REVOKE ALL ON FUNCTION enqueue_due_health_checks(integer, uuid) FROM PUBLIC")
    op.execute(f"GRANT EXECUTE ON FUNCTION enqueue_due_health_checks(integer, uuid) TO {APP_ROLE}")


def downgrade() -> None:
    op.execute("DROP FUNCTION IF EXISTS enqueue_due_health_checks(integer, uuid)")
    op.execute("DELETE FROM sync_jobs WHERE source_kind = 'connection'")
    op.execute("DELETE FROM source_sync_state WHERE source_kind = 'connection'")
    op.alter_column("source_sync_state", "project_id", nullable=False)
    op.alter_column("sync_jobs", "project_id", nullable=False)
    _kinds("source_sync_state", "ck_source_sync_state_kind", "'git','jira','confluence'")
    _kinds("sync_jobs", "ck_sync_jobs_kind", "'git','jira','confluence'")
    for table in SOURCE_TABLES:
        op.drop_constraint(f"ck_{table}_pause_cause", table, type_="check")
        for column in ("excluded_at", "excluded_by", "pause_cause"):
            op.drop_column(table, column)
