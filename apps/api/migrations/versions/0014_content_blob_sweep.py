"""Daily per-tenant sweep of orphan content blobs (INPROOM-162 retest / story INPROOM-27).

Revision ID: 0014_content_blob_sweep
Revises: 0013_content_purge_schedule
Create Date: 2026-11-25

Blobs live under a per-tenant prefix. A sync that writes a blob and then rolls back leaves no row anywhere, so
a schedule derived from stored rows (0013's per-project purge) can never reach it: the very first failed
ingestion of a tenant leaves nothing to schedule from. Orphan collection is therefore a tenant-level job:
- sync_jobs / source_sync_state accept kind 'sweep' (source_id = tenant id, no project);
- enqueue_due_sweeps(interval, tenant): SECURITY DEFINER, one coalesced sweep per due tenant (every tenant;
  a sweep of an empty prefix is a single cheap listing);
- tenants gets the owner-only sync_maintenance policy the function needs.
"""

from alembic import op

revision = "0014_content_blob_sweep"
down_revision = "0013_content_purge_schedule"
branch_labels = None
depends_on = None

APP_ROLE = "inproom_app"
SIGNATURE = "enqueue_due_sweeps(integer, uuid)"
KINDS_OLD = "'git','jira','confluence','connection','migration','derive','purge'"
KINDS_NEW = "'git','jira','confluence','connection','migration','derive','purge','sweep'"
FUNCTION = """
CREATE FUNCTION enqueue_due_sweeps(p_interval_seconds integer, p_tenant uuid DEFAULT NULL)
RETURNS integer
LANGUAGE plpgsql SECURITY DEFINER SET search_path = public, pg_temp AS $$
DECLARE
    queued integer := 0;
BEGIN
    WITH due AS (
        SELECT t.id AS tenant_id
          FROM tenants t
          LEFT JOIN source_sync_state st ON st.source_kind = 'sweep' AND st.source_id = t.id
         WHERE (st.next_due_at IS NULL OR st.next_due_at <= now())
           AND (p_tenant IS NULL OR t.id = p_tenant)
    ), inserted AS (
        INSERT INTO sync_jobs (tenant_id, project_id, source_kind, source_id, reason, mode)
        SELECT tenant_id, NULL, 'sweep', tenant_id, 'schedule', 'incremental' FROM due
        ON CONFLICT (source_kind, source_id) WHERE status = 'QUEUED' DO NOTHING
        RETURNING 1
    ), scheduled AS (
        INSERT INTO source_sync_state (tenant_id, project_id, source_kind, source_id, next_due_at)
        SELECT tenant_id, NULL, 'sweep', tenant_id, now() + make_interval(secs => p_interval_seconds)
          FROM due
        ON CONFLICT (source_kind, source_id) DO UPDATE SET next_due_at = EXCLUDED.next_due_at
        RETURNING 1
    )
    SELECT count(*) INTO queued FROM inserted;
    RETURN queued;
END;
$$;
"""


def _kinds(kinds: str) -> None:
    for table, name in (
        ("sync_jobs", "ck_sync_jobs_kind"),
        ("source_sync_state", "ck_source_sync_state_kind"),
    ):
        op.drop_constraint(name, table, type_="check")
        op.create_check_constraint(name, table, f"source_kind IN ({kinds})")


def upgrade() -> None:
    _kinds(KINDS_NEW)
    op.execute("CREATE POLICY sync_maintenance ON tenants TO CURRENT_USER USING (true) WITH CHECK (true)")
    op.execute(FUNCTION)
    op.execute(f"REVOKE ALL ON FUNCTION {SIGNATURE} FROM PUBLIC")
    op.execute(f"GRANT EXECUTE ON FUNCTION {SIGNATURE} TO {APP_ROLE}")


def downgrade() -> None:
    op.execute(f"DROP FUNCTION IF EXISTS {SIGNATURE}")
    op.execute("DROP POLICY IF EXISTS sync_maintenance ON tenants")
    op.execute("DELETE FROM sync_jobs WHERE source_kind = 'sweep'")
    op.execute("DELETE FROM source_sync_state WHERE source_kind = 'sweep'")
    _kinds(KINDS_OLD)
