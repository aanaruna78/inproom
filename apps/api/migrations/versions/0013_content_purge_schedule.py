"""Schedule the content retention purge independently of source syncs (INPROOM-162 / story INPROOM-27).

Revision ID: 0013_content_purge_schedule
Revises: 0012_content_store
Create Date: 2026-11-25

enqueue_due_purges(interval, tenant): SECURITY DEFINER, like the INPROOM-25 queue functions. It queues one
coalesced `purge` job (source_id = project id) for every project that holds stored content and is due,
whatever the state of its sources: removed, paused, excluded, invalid credentials, or simply quiet. This is
when retention matters most. The worker's schedule tick calls it.
FORCE ROW LEVEL SECURITY applies to the function's owner too, so source_objects gets the owner-only
sync_maintenance policy (as 0007 did for the tables its functions touch).
"""

from alembic import op

revision = "0013_content_purge_schedule"
down_revision = "0012_content_store"
branch_labels = None
depends_on = None

APP_ROLE = "inproom_app"
SIGNATURE = "enqueue_due_purges(integer, uuid)"
FUNCTION = """
CREATE FUNCTION enqueue_due_purges(p_interval_seconds integer, p_tenant uuid DEFAULT NULL)
RETURNS integer
LANGUAGE plpgsql SECURITY DEFINER SET search_path = public, pg_temp AS $$
DECLARE
    queued integer := 0;
BEGIN
    WITH due AS (
        SELECT p.tenant_id, p.id AS project_id
          FROM projects p
          LEFT JOIN source_sync_state st ON st.source_kind = 'purge' AND st.source_id = p.id
         WHERE (st.next_due_at IS NULL OR st.next_due_at <= now())
           AND (p_tenant IS NULL OR p.tenant_id = p_tenant)
           AND EXISTS (SELECT 1 FROM source_objects o
                        WHERE o.tenant_id = p.tenant_id AND o.project_id = p.id)
    ), inserted AS (
        INSERT INTO sync_jobs (tenant_id, project_id, source_kind, source_id, reason, mode)
        SELECT tenant_id, project_id, 'purge', project_id, 'schedule', 'incremental' FROM due
        ON CONFLICT (source_kind, source_id) WHERE status = 'QUEUED' DO NOTHING
        RETURNING 1
    ), scheduled AS (
        INSERT INTO source_sync_state (tenant_id, project_id, source_kind, source_id, next_due_at)
        SELECT tenant_id, project_id, 'purge', project_id, now() + make_interval(secs => p_interval_seconds)
          FROM due
        ON CONFLICT (source_kind, source_id) DO UPDATE SET next_due_at = EXCLUDED.next_due_at
        RETURNING 1
    )
    -- (data-modifying CTEs always run, so `scheduled` advances next_due_at even though it is not selected)
    SELECT count(*) INTO queued FROM inserted;
    RETURN queued;
END;
$$;
"""


def upgrade() -> None:
    op.execute(
        "CREATE POLICY sync_maintenance ON source_objects TO CURRENT_USER USING (true) WITH CHECK (true)"
    )
    op.execute(FUNCTION)
    op.execute(f"REVOKE ALL ON FUNCTION {SIGNATURE} FROM PUBLIC")
    op.execute(f"GRANT EXECUTE ON FUNCTION {SIGNATURE} TO {APP_ROLE}")


def downgrade() -> None:
    op.execute(f"DROP FUNCTION IF EXISTS {SIGNATURE}")
    op.execute("DROP POLICY IF EXISTS sync_maintenance ON source_objects")
