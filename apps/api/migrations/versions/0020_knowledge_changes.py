"""Knowledge change feed (INPROOM-186 / story INPROOM-31).

Revision ID: 0020_knowledge_changes
Revises: 0019_source_policies
Create Date: 2026-11-26

`knowledge_changes` is a per-project, ordered, gap-free feed of what became unservable (`invalidated`),
servable again (`restored`) or got new content (`refreshed`). Consumers that keep their own copies (retrieval
indexes, caches) resume from a cursor (`seq`).

Entries are written by `knowledge_append()`, in the SAME transaction as the state change, so the feed can
never disagree with what the read paths serve:
- item scope: triggers on source_objects (status into or out of CURRENT; a new current revision) and on
  source_revisions (bytes purged by retention);
- source scope: triggers on the project source tables (status into or out of ACTIVE; removal; git branches
  deselected or selected again); and application code for setting-driven changes (the classification cap,
  uploads disabled), which calls the same function.

`knowledge_heads` holds each project's last seq. Allocation updates that row, so it is ordered and gap-free (a
rolled-back transaction rolls its seqs back too). The first entry of a transaction also queues one outbox
event `knowledge.changed` {project_id, from_seq}. Entries carry identifiers only, never titles or content.
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import UUID

revision = "0020_knowledge_changes"
down_revision = "0019_source_policies"
branch_labels = None
depends_on = None

APP_ROLE = "inproom_app"
SOURCE_TABLES = {
    "project_confluence_sources": "confluence",
    "project_jira_sources": "jira",
    "project_repositories": "git",
}


def _rls(table: str) -> None:
    op.execute(f"GRANT SELECT, INSERT, UPDATE ON {table} TO {APP_ROLE}")
    op.execute(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY")
    op.execute(f"ALTER TABLE {table} FORCE ROW LEVEL SECURITY")
    op.execute(
        f"CREATE POLICY tenant_isolation ON {table} "
        "USING (tenant_id = current_tenant()) WITH CHECK (tenant_id = current_tenant())"
    )


def upgrade() -> None:
    op.create_table(
        "knowledge_heads",
        sa.Column("project_id", UUID(as_uuid=True), primary_key=True),
        sa.Column("tenant_id", UUID(as_uuid=True), nullable=False),
        sa.Column("seq", sa.BigInteger, nullable=False, server_default="0"),
        sa.Column("txid", sa.BigInteger),
        sa.ForeignKeyConstraint(
            ["tenant_id", "project_id"],
            ["projects.tenant_id", "projects.id"],
            name="fk_knowledge_heads_project",
        ),
    )
    op.create_table(
        "knowledge_changes",
        sa.Column("tenant_id", UUID(as_uuid=True), nullable=False),
        sa.Column("project_id", UUID(as_uuid=True), nullable=False),
        sa.Column("seq", sa.BigInteger, nullable=False),
        sa.Column("at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("change", sa.Text, nullable=False),
        sa.Column("scope", sa.Text, nullable=False),
        sa.Column("source_kind", sa.Text, nullable=False),
        sa.Column("source_id", UUID(as_uuid=True), nullable=False),
        sa.Column("external_id", sa.Text),
        sa.Column("branch", sa.Text),
        sa.Column("reason", sa.Text, nullable=False),
        sa.Column("revision_seq", sa.Integer),
        sa.PrimaryKeyConstraint("project_id", "seq", name="pk_knowledge_changes"),
        sa.ForeignKeyConstraint(
            ["tenant_id", "project_id"],
            ["projects.tenant_id", "projects.id"],
            name="fk_knowledge_changes_project",
        ),
        sa.CheckConstraint(
            "change IN ('invalidated','restored','refreshed')", name="ck_knowledge_changes_change"
        ),
        sa.CheckConstraint("scope IN ('item','source')", name="ck_knowledge_changes_scope"),
        sa.CheckConstraint(
            "(scope = 'item') = (external_id IS NOT NULL)", name="ck_knowledge_changes_item_has_external_id"
        ),
    )
    _rls("knowledge_heads")
    _rls("knowledge_changes")
    op.execute(
        """
        CREATE FUNCTION knowledge_append(
            p_tenant uuid, p_project uuid, p_change text, p_scope text, p_kind text, p_source uuid,
            p_external text, p_branch text, p_reason text, p_revision_seq integer
        ) RETURNS bigint LANGUAGE plpgsql AS $$
        DECLARE
            v_seq bigint;
            v_tx bigint;
        BEGIN
            INSERT INTO knowledge_heads (project_id, tenant_id) VALUES (p_project, p_tenant)
            ON CONFLICT (project_id) DO NOTHING;
            SELECT seq, txid INTO v_seq, v_tx FROM knowledge_heads WHERE project_id = p_project FOR UPDATE;
            v_seq := v_seq + 1;
            UPDATE knowledge_heads SET seq = v_seq, txid = txid_current() WHERE project_id = p_project;
            INSERT INTO knowledge_changes (tenant_id, project_id, seq, change, scope, source_kind, source_id,
                                           external_id, branch, reason, revision_seq)
            VALUES (p_tenant, p_project, v_seq, p_change, p_scope, p_kind, p_source,
                    p_external, p_branch, p_reason, p_revision_seq);
            IF v_tx IS DISTINCT FROM txid_current() THEN  -- this transaction's first entry
                INSERT INTO outbox_events (tenant_id, type, payload)
                VALUES (p_tenant, 'knowledge.changed',
                        jsonb_build_object('project_id', p_project, 'from_seq', v_seq));
            END IF;
            RETURN v_seq;
        END $$
        """
    )
    op.execute(f"GRANT EXECUTE ON FUNCTION knowledge_append TO {APP_ROLE}")
    op.execute(
        """
        CREATE FUNCTION knowledge_item_changed() RETURNS trigger LANGUAGE plpgsql AS $$
        DECLARE
            v_seq integer;
        BEGIN
            IF OLD.status IS DISTINCT FROM NEW.status
               AND (OLD.status = 'CURRENT' OR NEW.status = 'CURRENT') THEN
                IF NEW.status = 'CURRENT' THEN
                    PERFORM knowledge_append(NEW.tenant_id, NEW.project_id, 'restored', 'item',
                        NEW.source_kind, NEW.source_id, NEW.external_id, NULL, OLD.status, NULL);
                ELSE
                    PERFORM knowledge_append(NEW.tenant_id, NEW.project_id, 'invalidated', 'item',
                        NEW.source_kind, NEW.source_id, NEW.external_id, NULL, NEW.status, NULL);
                END IF;
            END IF;
            IF OLD.current_revision_id IS DISTINCT FROM NEW.current_revision_id
               AND NEW.current_revision_id IS NOT NULL AND NEW.status = 'CURRENT' THEN
                SELECT seq INTO v_seq FROM source_revisions WHERE id = NEW.current_revision_id;
                PERFORM knowledge_append(NEW.tenant_id, NEW.project_id, 'refreshed', 'item', NEW.source_kind,
                    NEW.source_id, NEW.external_id, NULL, 'NEW_REVISION', v_seq);
            END IF;
            RETURN NULL;
        END $$
        """
    )
    op.execute(
        "CREATE TRIGGER knowledge_item_changed AFTER UPDATE OF status, current_revision_id ON source_objects "
        "FOR EACH ROW WHEN (OLD.status IS DISTINCT FROM NEW.status "
        "OR OLD.current_revision_id IS DISTINCT FROM NEW.current_revision_id) "
        "EXECUTE FUNCTION knowledge_item_changed()"
    )
    op.execute(
        """
        CREATE FUNCTION knowledge_revision_purged() RETURNS trigger LANGUAGE plpgsql AS $$
        DECLARE
            o record;
        BEGIN
            SELECT tenant_id, project_id, source_kind, source_id, external_id INTO o
            FROM source_objects WHERE id = NEW.object_id;
            IF FOUND THEN
                PERFORM knowledge_append(o.tenant_id, o.project_id, 'invalidated', 'item', o.source_kind,
                    o.source_id, o.external_id, NULL, 'PURGED', NEW.seq);
            END IF;
            RETURN NULL;
        END $$
        """
    )
    op.execute(
        "CREATE TRIGGER knowledge_revision_purged AFTER UPDATE OF purged_at ON source_revisions "
        "FOR EACH ROW WHEN (OLD.purged_at IS NULL AND NEW.purged_at IS NOT NULL) "
        "EXECUTE FUNCTION knowledge_revision_purged()"
    )
    op.execute(
        """
        CREATE FUNCTION knowledge_source_changed() RETURNS trigger LANGUAGE plpgsql AS $$
        DECLARE
            v_kind text := TG_ARGV[0];
            b text;
        BEGIN
            IF TG_OP = 'DELETE' THEN
                IF EXISTS (SELECT 1 FROM projects WHERE id = OLD.project_id) THEN
                    PERFORM knowledge_append(OLD.tenant_id, OLD.project_id, 'invalidated', 'source', v_kind,
                        OLD.id, NULL, NULL, 'SOURCE_REMOVED', NULL);
                END IF;
                RETURN NULL;
            END IF;
            IF OLD.status IS DISTINCT FROM NEW.status
               AND (OLD.status = 'ACTIVE' OR NEW.status = 'ACTIVE') THEN
                PERFORM knowledge_append(NEW.tenant_id, NEW.project_id,
                    CASE WHEN NEW.status = 'ACTIVE' THEN 'restored' ELSE 'invalidated' END, 'source', v_kind,
                    NEW.id, NULL, NULL, 'SOURCE_' || CASE WHEN NEW.status = 'ACTIVE' THEN OLD.status
                                                          ELSE NEW.status END, NULL);
            END IF;
            -- nested: plpgsql does not short-circuit, and only repositories have branches
            IF v_kind = 'git' THEN
            IF OLD.branches IS DISTINCT FROM NEW.branches THEN
                FOR b IN SELECT x FROM unnest(OLD.branches) x EXCEPT SELECT y FROM unnest(NEW.branches) y
                         ORDER BY 1 LOOP
                    PERFORM knowledge_append(NEW.tenant_id, NEW.project_id, 'invalidated', 'source', v_kind,
                        NEW.id, NULL, b, 'BRANCH_DESELECTED', NULL);
                END LOOP;
                FOR b IN SELECT y FROM unnest(NEW.branches) y EXCEPT SELECT x FROM unnest(OLD.branches) x
                         ORDER BY 1 LOOP
                    PERFORM knowledge_append(NEW.tenant_id, NEW.project_id, 'restored', 'source', v_kind,
                        NEW.id, NULL, b, 'BRANCH_SELECTED', NULL);
                END LOOP;
            END IF;
            END IF;
            RETURN NULL;
        END $$
        """
    )
    for table, kind in SOURCE_TABLES.items():
        columns = "status, branches" if kind == "git" else "status"
        op.execute(
            f"CREATE TRIGGER knowledge_source_changed AFTER UPDATE OF {columns} OR DELETE ON {table} "
            f"FOR EACH ROW EXECUTE FUNCTION knowledge_source_changed('{kind}')"
        )


def downgrade() -> None:
    for table in SOURCE_TABLES:
        op.execute(f"DROP TRIGGER IF EXISTS knowledge_source_changed ON {table}")
    op.execute("DROP TRIGGER IF EXISTS knowledge_revision_purged ON source_revisions")
    op.execute("DROP TRIGGER IF EXISTS knowledge_item_changed ON source_objects")
    op.execute("DROP FUNCTION IF EXISTS knowledge_source_changed()")
    op.execute("DROP FUNCTION IF EXISTS knowledge_revision_purged()")
    op.execute("DROP FUNCTION IF EXISTS knowledge_item_changed()")
    op.execute(
        "DROP FUNCTION IF EXISTS "
        "knowledge_append(uuid, uuid, text, text, text, uuid, text, text, text, integer)"
    )
    for table in ("knowledge_changes", "knowledge_heads"):
        op.execute(f"REVOKE ALL ON {table} FROM {APP_ROLE}")
        op.drop_table(table)
