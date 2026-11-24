"""Durable source sync jobs and per-source sync state (INPROOM-141 / story INPROOM-25).

Revision ID: 0007_sync_jobs
Revises: 0006_confluence_sources
Create Date: 2026-11-24

Both tables are tenant-scoped with FORCE ROW LEVEL SECURITY. Workers must find work across tenants, so three
narrow SECURITY DEFINER functions (owned by the migration owner, fixed search_path, EXECUTE only for the app
role) do exactly that and nothing more:
- claim_sync_job(worker, lease)      -> (job_id, tenant_id) of one due QUEUED job, now RUNNING under a lease;
- requeue_expired_sync_jobs()        -> recovers RUNNING jobs whose lease expired (crashed worker);
- enqueue_due_syncs(interval, full)  -> queues a job for every ACTIVE source that is due.
Each takes an optional tenant filter (NULL = all tenants) so operators and tests can drain one tenant.
All real work then happens in a tenant session for the job's tenant.
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB, UUID

revision = "0007_sync_jobs"
down_revision = "0006_confluence_sources"
branch_labels = None
depends_on = None

APP_ROLE = "inproom_app"
TABLES = {"sync_jobs": "SELECT, INSERT, UPDATE", "source_sync_state": "SELECT, INSERT, UPDATE, DELETE"}
MAX_ATTEMPTS = 6

FUNCTIONS = f"""
CREATE FUNCTION claim_sync_job(p_worker text, p_lease_seconds integer, p_tenant uuid DEFAULT NULL)
RETURNS TABLE (job_id uuid, job_tenant uuid)
LANGUAGE sql SECURITY DEFINER SET search_path = public, pg_temp AS $$
    UPDATE sync_jobs j
       SET status = 'RUNNING', locked_by = p_worker, attempts = j.attempts + 1,
           locked_until = now() + make_interval(secs => p_lease_seconds), started_at = now()
     WHERE j.id = (
           SELECT id FROM sync_jobs
            WHERE status = 'QUEUED' AND run_after <= now() AND (p_tenant IS NULL OR tenant_id = p_tenant)
            ORDER BY run_after, created_at
            FOR UPDATE SKIP LOCKED
            LIMIT 1)
    RETURNING j.id, j.tenant_id;
$$;

CREATE FUNCTION requeue_expired_sync_jobs(p_tenant uuid DEFAULT NULL)
RETURNS integer
LANGUAGE plpgsql SECURITY DEFINER SET search_path = public, pg_temp AS $$
DECLARE
    expired record;
    recovered integer := 0;
BEGIN
    FOR expired IN
        SELECT * FROM sync_jobs WHERE status = 'RUNNING' AND locked_until < now()
           AND (p_tenant IS NULL OR tenant_id = p_tenant) FOR UPDATE SKIP LOCKED
    LOOP
        IF expired.attempts >= {MAX_ATTEMPTS} THEN
            UPDATE sync_jobs SET status = 'DEAD', locked_by = NULL, locked_until = NULL, finished_at = now(),
                   last_error = jsonb_build_object('code', 'LEASE_EXPIRED', 'message',
                                                   'worker stopped while running; attempts exhausted')
             WHERE id = expired.id;
        ELSIF EXISTS (SELECT 1 FROM sync_jobs WHERE status = 'QUEUED'
                         AND source_kind = expired.source_kind AND source_id = expired.source_id) THEN
            -- a newer queued job for the same source will do the same work
            UPDATE sync_jobs SET status = 'SKIPPED', locked_by = NULL, locked_until = NULL,
                   finished_at = now(),
                   last_error = jsonb_build_object('code', 'LEASE_EXPIRED',
                                                   'message', 'superseded by a queued job')
             WHERE id = expired.id;
            UPDATE sync_jobs SET triggers = triggers + expired.triggers
             WHERE status = 'QUEUED' AND source_kind = expired.source_kind AND source_id = expired.source_id;
        ELSE
            UPDATE sync_jobs SET status = 'QUEUED', locked_by = NULL, locked_until = NULL, run_after = now(),
                   reason = 'recovery'
             WHERE id = expired.id;
        END IF;
        recovered := recovered + 1;
    END LOOP;
    RETURN recovered;
END;
$$;

CREATE FUNCTION enqueue_due_syncs(
    p_interval_seconds integer, p_full_interval_seconds integer, p_tenant uuid DEFAULT NULL)
RETURNS integer
LANGUAGE plpgsql SECURITY DEFINER SET search_path = public, pg_temp AS $$
DECLARE
    queued integer := 0;
BEGIN
    WITH sources AS (
        SELECT 'git'::text AS kind, r.id, r.tenant_id, r.project_id, r.connection_id
          FROM project_repositories r WHERE r.status = 'ACTIVE'
        UNION ALL
        SELECT 'jira', s.id, s.tenant_id, s.project_id, s.connection_id
          FROM project_jira_sources s WHERE s.status = 'ACTIVE'
        UNION ALL
        SELECT 'confluence', s.id, s.tenant_id, s.project_id, s.connection_id
          FROM project_confluence_sources s WHERE s.status = 'ACTIVE'
    ), due AS (
        SELECT s.*, st.last_full_at
          FROM sources s
          JOIN source_connections c ON c.tenant_id = s.tenant_id AND c.id = s.connection_id
                                   AND c.status IN ('ACTIVE', 'DEGRADED')
          JOIN projects p ON p.tenant_id = s.tenant_id AND p.id = s.project_id AND p.status <> 'ARCHIVED'
          LEFT JOIN source_sync_state st ON st.source_kind = s.kind AND st.source_id = s.id
         WHERE (st.next_due_at IS NULL OR st.next_due_at <= now())
           AND (p_tenant IS NULL OR s.tenant_id = p_tenant)
    ), inserted AS (
        INSERT INTO sync_jobs (tenant_id, project_id, source_kind, source_id, reason, mode)
        SELECT tenant_id, project_id, kind, id, 'schedule',
               CASE WHEN last_full_at IS NULL
                         OR last_full_at < now() - make_interval(secs => p_full_interval_seconds)
                    THEN 'full' ELSE 'incremental' END
          FROM due
        ON CONFLICT (source_kind, source_id) WHERE status = 'QUEUED' DO NOTHING
        RETURNING 1
    ), scheduled AS (
        INSERT INTO source_sync_state (tenant_id, project_id, source_kind, source_id, next_due_at)
        SELECT tenant_id, project_id, kind, id, now() + make_interval(secs => p_interval_seconds) FROM due
        ON CONFLICT (source_kind, source_id) DO UPDATE SET next_due_at = EXCLUDED.next_due_at
        RETURNING 1
    )
    -- (data-modifying CTEs always run, so `scheduled` advances next_due_at even though it is not selected)
    SELECT count(*) INTO queued FROM inserted;
    RETURN queued;
END;
$$;
"""
# FORCE ROW LEVEL SECURITY applies to table owners too, so the definer functions (owned by the migration
# owner) need a policy of their own on the tables they read or write. It is granted to the owner role only -
# the role that can already ALTER these tables - so it widens nothing; the app role stays tenant-bound.
OWNER_POLICY_TABLES = (
    "sync_jobs",
    "source_sync_state",
    "projects",
    "source_connections",
    "project_repositories",
    "project_jira_sources",
    "project_confluence_sources",
)
SIGNATURES = (
    "claim_sync_job(text, integer, uuid)",
    "requeue_expired_sync_jobs(uuid)",
    "enqueue_due_syncs(integer, integer, uuid)",
)


def _ts(name: str, nullable: bool = True) -> sa.Column:
    return sa.Column(
        name,
        sa.DateTime(timezone=True),
        nullable=nullable,
        server_default=None if nullable else sa.func.now(),
    )


def upgrade() -> None:
    op.create_table(
        "sync_jobs",
        sa.Column("id", UUID(as_uuid=True), primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column("tenant_id", UUID(as_uuid=True), sa.ForeignKey("tenants.id"), nullable=False),
        sa.Column("project_id", UUID(as_uuid=True), nullable=False),
        sa.Column("source_kind", sa.Text, nullable=False),
        sa.Column("source_id", UUID(as_uuid=True), nullable=False),
        sa.Column("reason", sa.Text, nullable=False),
        sa.Column("mode", sa.Text, nullable=False, server_default="incremental"),
        sa.Column("status", sa.Text, nullable=False, server_default="QUEUED"),
        sa.Column("attempts", sa.Integer, nullable=False, server_default="0"),
        sa.Column("triggers", sa.Integer, nullable=False, server_default="1"),
        sa.Column("hints", JSONB, nullable=False, server_default=sa.text("'{}'::jsonb")),
        _ts("run_after", nullable=False),
        sa.Column("locked_by", sa.Text),
        _ts("locked_until"),
        sa.Column("last_error", JSONB),
        sa.Column("result", JSONB, nullable=False, server_default=sa.text("'{}'::jsonb")),
        _ts("created_at", nullable=False),
        _ts("started_at"),
        _ts("finished_at"),
        sa.CheckConstraint("source_kind IN ('git','jira','confluence')", name="ck_sync_jobs_kind"),
        sa.CheckConstraint(
            "reason IN ('webhook','schedule','manual','recovery')", name="ck_sync_jobs_reason"
        ),
        sa.CheckConstraint("mode IN ('incremental','full')", name="ck_sync_jobs_mode"),
        sa.CheckConstraint(
            "status IN ('QUEUED','RUNNING','SUCCEEDED','FAILED','DEAD','SKIPPED')", name="ck_sync_jobs_status"
        ),
    )
    op.create_index(
        "uq_sync_jobs_queued",
        "sync_jobs",
        ["source_kind", "source_id"],
        unique=True,
        postgresql_where=sa.text("status = 'QUEUED'"),
    )
    op.create_index("ix_sync_jobs_due", "sync_jobs", ["status", "run_after"])
    op.create_index("ix_sync_jobs_project", "sync_jobs", ["tenant_id", "project_id", "created_at"])
    op.create_table(
        "source_sync_state",
        sa.Column("tenant_id", UUID(as_uuid=True), sa.ForeignKey("tenants.id"), nullable=False),
        sa.Column("project_id", UUID(as_uuid=True), nullable=False),
        sa.Column("source_kind", sa.Text, nullable=False),
        sa.Column("source_id", UUID(as_uuid=True), nullable=False),
        sa.Column("watermark", JSONB, nullable=False, server_default=sa.text("'{}'::jsonb")),
        _ts("last_success_at"),
        _ts("last_attempt_at"),
        _ts("last_full_at"),
        _ts("next_due_at"),
        sa.Column("consecutive_failures", sa.Integer, nullable=False, server_default="0"),
        sa.Column("last_error", JSONB),
        _ts("updated_at", nullable=False),
        sa.PrimaryKeyConstraint("source_kind", "source_id", name="pk_source_sync_state"),
        sa.CheckConstraint("source_kind IN ('git','jira','confluence')", name="ck_source_sync_state_kind"),
    )
    for table, grants in TABLES.items():
        op.execute(f"GRANT {grants} ON {table} TO {APP_ROLE}")
        op.execute(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY")
        op.execute(f"ALTER TABLE {table} FORCE ROW LEVEL SECURITY")
        op.execute(
            f"CREATE POLICY tenant_isolation ON {table} "
            "USING (tenant_id = current_tenant()) WITH CHECK (tenant_id = current_tenant())"
        )
    for table in OWNER_POLICY_TABLES:
        op.execute(
            f"CREATE POLICY sync_maintenance ON {table} TO CURRENT_USER USING (true) WITH CHECK (true)"
        )
    op.execute(FUNCTIONS)
    for signature in SIGNATURES:
        op.execute(f"REVOKE ALL ON FUNCTION {signature} FROM PUBLIC")
        op.execute(f"GRANT EXECUTE ON FUNCTION {signature} TO {APP_ROLE}")


def downgrade() -> None:
    for signature in SIGNATURES:
        op.execute(f"DROP FUNCTION IF EXISTS {signature}")
    for table in OWNER_POLICY_TABLES:
        op.execute(f"DROP POLICY IF EXISTS sync_maintenance ON {table}")
    for table in reversed(list(TABLES)):
        op.execute(f"REVOKE ALL ON {table} FROM {APP_ROLE}")
        op.drop_table(table)
