"""SQLAlchemy Core table definitions. Migrations are hand-written in migrations/versions; keep both in sync.

Every tenant-scoped table carries tenant_id and is protected by FORCE ROW LEVEL SECURITY (see 0001 migration).
"""

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    Column,
    Computed,
    Date,
    DateTime,
    ForeignKey,
    ForeignKeyConstraint,
    Identity,
    Index,
    Integer,
    LargeBinary,
    MetaData,
    PrimaryKeyConstraint,
    SmallInteger,
    Table,
    Text,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import ARRAY, JSONB, TSVECTOR, UUID
from sqlalchemy.types import UserDefinedType

metadata = MetaData()

APP_ROLE = "inproom_app"

TENANT_SCOPED_TABLES = (
    "tenants",
    "workspaces",
    "projects",
    "project_members",
    "project_settings_versions",
    "audit_events",
    "outbox_events",
    "idempotency_keys",
    "tenant_users",
    "source_connections",
    "connection_secrets",
    "project_repositories",
    "webhook_deliveries",
    "project_jira_sources",
    "jira_schema_snapshots",
    "project_confluence_sources",
    "confluence_pages",
    "confluence_page_versions",
    "sync_jobs",
    "source_sync_state",
    "migration_plans",
    "migration_runs",
    "migration_issues",
    "source_objects",
    "source_revisions",
    "derivatives",
    "code_snapshots",
    "code_symbols",
    "source_policies",
    "knowledge_heads",
    "knowledge_changes",
    "knowledge_chunks",
    "chunk_embeddings",
    "chunk_embedding_failures",
)

# Project roles allowed in project_members (mirrors app.authz.policy.PROJECT_ROLES; checked by a unit test).
PROJECT_ROLE_VALUES = (
    "project_admin",
    "product_owner",
    "tech_lead",
    "developer",
    "reviewer",
    "qa_lead",
    "tester",
    "release_manager",
    "viewer",
)


class Vector(UserDefinedType):
    """pgvector's `vector` (INPROOM-204). Untyped in the table (dimensions vary by model); `Vector(n)` is used
    in casts so the query matches the per-model HNSW index expression. Values are bound as text."""

    cache_ok = True

    def __init__(self, dimensions: int | None = None):
        self.dimensions = dimensions

    def get_col_spec(self, **kw) -> str:
        return f"vector({int(self.dimensions)})" if self.dimensions else "vector"


def _id() -> Column:
    return Column("id", UUID(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()"))


def _created() -> Column:
    return Column("created_at", DateTime(timezone=True), nullable=False, server_default=func.now())


tenants = Table(
    "tenants",
    metadata,
    Column("id", UUID(as_uuid=True), primary_key=True),
    Column("name", Text, nullable=False),
    Column("residency_region", Text, nullable=False, server_default="default"),
    Column("status", Text, nullable=False, server_default="ACTIVE"),
    _created(),
    CheckConstraint("status IN ('ACTIVE','SUSPENDED')", name="ck_tenants_status"),
)

workspaces = Table(
    "workspaces",
    metadata,
    _id(),
    Column("tenant_id", UUID(as_uuid=True), ForeignKey("tenants.id"), nullable=False),
    Column("slug", Text, nullable=False),
    Column("name", Text, nullable=False),
    Column("created_by", Text, nullable=False),
    _created(),
    UniqueConstraint("tenant_id", "slug", name="uq_workspaces_tenant_slug"),
    UniqueConstraint("tenant_id", "id", name="uq_workspaces_tenant_id"),
    CheckConstraint("slug ~ '^[a-z][a-z0-9-]{1,39}$'", name="ck_workspaces_slug"),
)

projects = Table(
    "projects",
    metadata,
    _id(),
    Column("tenant_id", UUID(as_uuid=True), nullable=False),
    Column("workspace_id", UUID(as_uuid=True), nullable=False),
    Column("key", Text, nullable=False),
    Column("name", Text, nullable=False),
    Column("description", Text),
    Column("mode", Text, nullable=False),
    Column("status", Text, nullable=False),
    Column("owner_id", Text, nullable=False),
    Column("settings_version", Integer, nullable=False, server_default="1"),
    Column("version", Integer, nullable=False, server_default="1"),
    _created(),
    Column("updated_at", DateTime(timezone=True), nullable=False, server_default=func.now()),
    ForeignKeyConstraint(
        ["tenant_id", "workspace_id"], ["workspaces.tenant_id", "workspaces.id"], name="fk_projects_workspace"
    ),
    UniqueConstraint("tenant_id", "key", name="uq_projects_tenant_key"),
    UniqueConstraint("tenant_id", "id", name="uq_projects_tenant_id"),
    CheckConstraint("key ~ '^[A-Z][A-Z0-9]{1,9}$'", name="ck_projects_key"),
    CheckConstraint("mode IN ('NEW','MIGRATION')", name="ck_projects_mode"),
    CheckConstraint("status IN ('ACTIVE','PENDING_SOURCES','ARCHIVED')", name="ck_projects_status"),
    CheckConstraint("char_length(name) BETWEEN 3 AND 120", name="ck_projects_name"),
)

project_members = Table(
    "project_members",
    metadata,
    Column("tenant_id", UUID(as_uuid=True), nullable=False),
    Column("project_id", UUID(as_uuid=True), nullable=False),
    Column("user_id", Text, nullable=False),
    Column("role", Text, nullable=False),
    Column("added_by", Text, nullable=False),
    Column("added_at", DateTime(timezone=True), nullable=False, server_default=func.now()),
    PrimaryKeyConstraint("project_id", "user_id", "role", name="pk_project_members"),
    CheckConstraint(
        "role IN (" + ",".join(f"'{r}'" for r in PROJECT_ROLE_VALUES) + ")", name="ck_project_members_role"
    ),
    ForeignKeyConstraint(
        ["tenant_id", "project_id"], ["projects.tenant_id", "projects.id"], name="fk_members_project"
    ),
)

project_settings_versions = Table(
    "project_settings_versions",
    metadata,
    Column("tenant_id", UUID(as_uuid=True), nullable=False),
    Column("project_id", UUID(as_uuid=True), nullable=False),
    Column("version", Integer, nullable=False),
    Column("settings", JSONB, nullable=False),
    Column("content_sha256", Text, nullable=False),
    Column("created_by", Text, nullable=False),
    _created(),
    # INPROOM-102: schema of the stored document (1 = legacy, read as v2), note and changed paths per version.
    Column("schema_version", SmallInteger, nullable=False, server_default="1"),
    Column("change_note", Text),
    Column("changed_paths", ARRAY(Text), nullable=False, server_default=text("'{}'::text[]")),
    PrimaryKeyConstraint("project_id", "version", name="pk_project_settings_versions"),
    CheckConstraint("version >= 1", name="ck_settings_version_positive"),
    ForeignKeyConstraint(
        ["tenant_id", "project_id"], ["projects.tenant_id", "projects.id"], name="fk_settings_project"
    ),
)

audit_events = Table(
    "audit_events",
    metadata,
    _id(),
    Column("tenant_id", UUID(as_uuid=True), nullable=False),
    Column("project_id", UUID(as_uuid=True)),
    Column("actor_id", Text, nullable=False),
    Column("action", Text, nullable=False),
    Column("target_type", Text, nullable=False),
    Column("target_id", Text),
    Column("outcome", Text, nullable=False),
    Column("reason", Text),
    Column("payload_sha256", Text),
    Column("trace_id", Text),
    Column("occurred_at", DateTime(timezone=True), nullable=False, server_default=func.now()),
    CheckConstraint("outcome IN ('SUCCESS','DENIED','FAILED')", name="ck_audit_outcome"),
)

outbox_events = Table(
    "outbox_events",
    metadata,
    _id(),
    Column("tenant_id", UUID(as_uuid=True), nullable=False),
    Column("type", Text, nullable=False),
    Column("payload", JSONB, nullable=False),
    _created(),
    Column("published_at", DateTime(timezone=True)),
)

idempotency_keys = Table(
    "idempotency_keys",
    metadata,
    Column("tenant_id", UUID(as_uuid=True), nullable=False),
    Column("key", Text, nullable=False),
    Column("request_sha256", Text, nullable=False),
    Column("status", Text, nullable=False),
    Column("response_status", SmallInteger),
    Column("response_body", JSONB),
    _created(),
    PrimaryKeyConstraint("tenant_id", "key", name="pk_idempotency_keys"),
    CheckConstraint("status IN ('IN_PROGRESS','COMPLETED')", name="ck_idempotency_status"),
    CheckConstraint("char_length(key) BETWEEN 1 AND 128", name="ck_idempotency_key_len"),
)

# Tenant user directory (INPROOM-20): users are registered on GET /v1/me; members can only be added from here.
tenant_users = Table(
    "tenant_users",
    metadata,
    Column("tenant_id", UUID(as_uuid=True), ForeignKey("tenants.id"), nullable=False),
    Column("user_id", Text, nullable=False),
    Column("email", Text),
    Column("display_name", Text),
    Column("first_seen_at", DateTime(timezone=True), nullable=False, server_default=func.now()),
    Column("last_seen_at", DateTime(timezone=True), nullable=False, server_default=func.now()),
    PrimaryKeyConstraint("tenant_id", "user_id", name="pk_tenant_users"),
)

# ---------------------------------------------------------------- source connectors (INPROOM-108)

source_connections = Table(
    "source_connections",
    metadata,
    _id(),
    Column("tenant_id", UUID(as_uuid=True), ForeignKey("tenants.id"), nullable=False),
    Column("provider", Text, nullable=False),
    Column("kind", Text, nullable=False),
    Column("display_name", Text, nullable=False),
    Column("base_url", Text, nullable=False),
    Column("auth_type", Text, nullable=False),
    Column("account_login", Text),
    Column("granted_scopes", ARRAY(Text), nullable=False, server_default=text("'{}'::text[]")),
    Column("credential_fingerprint", Text),
    Column("status", Text, nullable=False),
    Column("health", JSONB, nullable=False, server_default=text("'{}'::jsonb")),
    Column("last_checked_at", DateTime(timezone=True)),
    Column("created_by", Text, nullable=False),
    _created(),
    Column("updated_at", DateTime(timezone=True), nullable=False, server_default=func.now()),
    Column("version", Integer, nullable=False, server_default="1"),
    UniqueConstraint("tenant_id", "id", name="uq_source_connections_tenant_id"),
    UniqueConstraint("tenant_id", "display_name", name="uq_source_connections_name"),
    CheckConstraint("provider IN ('github','jira','confluence')", name="ck_source_connections_provider"),
    CheckConstraint("kind IN ('git','jira','confluence')", name="ck_source_connections_kind"),
    CheckConstraint(
        "auth_type IN ('token','github_app','basic_token','pat')", name="ck_source_connections_auth"
    ),
    CheckConstraint(
        "status IN ('ACTIVE','DEGRADED','INVALID','REVOKED')", name="ck_source_connections_status"
    ),
)

connection_secrets = Table(
    "connection_secrets",
    metadata,
    Column("tenant_id", UUID(as_uuid=True), nullable=False),
    Column("connection_id", UUID(as_uuid=True), nullable=False),
    Column("purpose", Text, nullable=False),
    Column("key_id", Text, nullable=False),
    Column("nonce", LargeBinary, nullable=False),
    Column("ciphertext", LargeBinary, nullable=False),
    _created(),
    PrimaryKeyConstraint("connection_id", "purpose", name="pk_connection_secrets"),
    ForeignKeyConstraint(
        ["tenant_id", "connection_id"],
        ["source_connections.tenant_id", "source_connections.id"],
        name="fk_secrets_connection",
    ),
    CheckConstraint("purpose IN ('credential','webhook')", name="ck_connection_secrets_purpose"),
)

project_repositories = Table(
    "project_repositories",
    metadata,
    _id(),
    Column("tenant_id", UUID(as_uuid=True), nullable=False),
    Column("project_id", UUID(as_uuid=True), nullable=False),
    Column("connection_id", UUID(as_uuid=True), nullable=False),
    Column("provider_repo_id", Text, nullable=False),
    Column("full_name", Text, nullable=False),
    Column("private", Boolean, nullable=False),
    Column("default_branch", Text, nullable=False),
    Column("branches", ARRAY(Text), nullable=False),
    Column("permissions", JSONB, nullable=False, server_default=text("'{}'::jsonb")),
    Column("status", Text, nullable=False),
    Column("status_reason", Text),
    Column("pause_cause", Text),
    Column("excluded_by", Text),
    Column("excluded_at", DateTime(timezone=True)),
    Column("webhook_id", Text),
    Column("webhook_status", Text, nullable=False, server_default="none"),
    Column("selected_by", Text, nullable=False),
    Column("selected_at", DateTime(timezone=True), nullable=False, server_default=func.now()),
    Column("updated_at", DateTime(timezone=True), nullable=False, server_default=func.now()),
    Column("version", Integer, nullable=False, server_default="1"),
    UniqueConstraint("project_id", "connection_id", "provider_repo_id", name="uq_project_repositories_repo"),
    ForeignKeyConstraint(
        ["tenant_id", "project_id"], ["projects.tenant_id", "projects.id"], name="fk_project_repos_project"
    ),
    ForeignKeyConstraint(
        ["tenant_id", "connection_id"],
        ["source_connections.tenant_id", "source_connections.id"],
        name="fk_project_repos_connection",
    ),
    CheckConstraint("status IN ('ACTIVE','PAUSED')", name="ck_project_repositories_status"),
    CheckConstraint(
        "webhook_status IN ('none','registered','failed','not_configured')",
        name="ck_project_repositories_webhook",
    ),
    CheckConstraint("cardinality(branches) BETWEEN 1 AND 50", name="ck_project_repositories_branches"),
    CheckConstraint(
        "pause_cause IN ('access','connection_revoked','excluded')",
        name="ck_project_repositories_pause_cause",
    ),
)

webhook_deliveries = Table(
    "webhook_deliveries",
    metadata,
    Column("tenant_id", UUID(as_uuid=True), nullable=False),
    Column("connection_id", UUID(as_uuid=True), nullable=False),
    Column("delivery_id", Text, nullable=False),
    Column("event", Text, nullable=False),
    Column("provider_repo_id", Text),
    Column("outcome", Text, nullable=False),
    Column("received_at", DateTime(timezone=True), nullable=False, server_default=func.now()),
    PrimaryKeyConstraint("connection_id", "delivery_id", name="pk_webhook_deliveries"),
    ForeignKeyConstraint(
        ["tenant_id", "connection_id"],
        ["source_connections.tenant_id", "source_connections.id"],
        name="fk_deliveries_connection",
    ),
    CheckConstraint("outcome IN ('processed','ignored')", name="ck_webhook_deliveries_outcome"),
)


project_jira_sources = Table(
    "project_jira_sources",
    metadata,
    _id(),
    Column("tenant_id", UUID(as_uuid=True), nullable=False),
    Column("project_id", UUID(as_uuid=True), nullable=False),
    Column("connection_id", UUID(as_uuid=True), nullable=False),
    Column("jira_project_id", Text, nullable=False),
    Column("jira_key", Text, nullable=False),
    Column("name", Text, nullable=False),
    Column("status", Text, nullable=False),
    Column("status_reason", Text),
    Column("pause_cause", Text),
    Column("excluded_by", Text),
    Column("excluded_at", DateTime(timezone=True)),
    Column("capabilities", JSONB, nullable=False, server_default=text("'{}'::jsonb")),
    Column("mapping", JSONB, nullable=False, server_default=text("'{}'::jsonb")),
    Column("mapping_version", Integer, nullable=False, server_default="1"),
    Column("snapshot_version", Integer, nullable=False, server_default="0"),
    Column("warnings", JSONB, nullable=False, server_default=text("'[]'::jsonb")),
    # INPROOM-188: issues under a Jira security level are withheld unless allowed here
    Column(
        "security",
        JSONB,
        nullable=False,
        server_default=text("""'{"include_secured": false, "allowed_levels": []}'::jsonb"""),
    ),
    Column("selected_by", Text, nullable=False),
    Column("selected_at", DateTime(timezone=True), nullable=False, server_default=func.now()),
    Column("updated_at", DateTime(timezone=True), nullable=False, server_default=func.now()),
    Column("version", Integer, nullable=False, server_default="1"),
    UniqueConstraint("tenant_id", "id", name="uq_project_jira_sources_tenant_id"),
    UniqueConstraint(
        "project_id", "connection_id", "jira_project_id", name="uq_project_jira_sources_project"
    ),
    ForeignKeyConstraint(
        ["tenant_id", "project_id"], ["projects.tenant_id", "projects.id"], name="fk_jira_sources_project"
    ),
    ForeignKeyConstraint(
        ["tenant_id", "connection_id"],
        ["source_connections.tenant_id", "source_connections.id"],
        name="fk_jira_sources_connection",
    ),
    CheckConstraint("status IN ('ACTIVE','PAUSED')", name="ck_project_jira_sources_status"),
    CheckConstraint(
        "pause_cause IN ('access','connection_revoked','excluded')",
        name="ck_project_jira_sources_pause_cause",
    ),
)

jira_schema_snapshots = Table(
    "jira_schema_snapshots",
    metadata,
    _id(),
    Column("tenant_id", UUID(as_uuid=True), nullable=False),
    Column("source_id", UUID(as_uuid=True), nullable=False),
    Column("version", Integer, nullable=False),
    Column("fingerprint", Text, nullable=False),
    Column("schema", JSONB, nullable=False),
    Column("diff", JSONB, nullable=False, server_default=text("'{}'::jsonb")),
    Column("discovered_by", Text, nullable=False),
    Column("discovered_at", DateTime(timezone=True), nullable=False, server_default=func.now()),
    UniqueConstraint("source_id", "version", name="uq_jira_schema_snapshots_version"),
    ForeignKeyConstraint(
        ["tenant_id", "source_id"],
        ["project_jira_sources.tenant_id", "project_jira_sources.id"],
        name="fk_jira_snapshots_source",
        ondelete="CASCADE",
    ),
    CheckConstraint("version >= 1", name="ck_jira_schema_snapshots_version"),
)

project_confluence_sources = Table(
    "project_confluence_sources",
    metadata,
    _id(),
    Column("tenant_id", UUID(as_uuid=True), nullable=False),
    Column("project_id", UUID(as_uuid=True), nullable=False),
    Column("connection_id", UUID(as_uuid=True), nullable=False),
    Column("space_id", Text, nullable=False),
    Column("space_key", Text, nullable=False),
    Column("name", Text, nullable=False),
    Column("filters", JSONB, nullable=False, server_default=text("'{}'::jsonb")),
    Column("capabilities", JSONB, nullable=False, server_default=text("'{}'::jsonb")),
    Column("status", Text, nullable=False),
    Column("status_reason", Text),
    Column("pause_cause", Text),
    Column("excluded_by", Text),
    Column("excluded_at", DateTime(timezone=True)),
    Column("warnings", JSONB, nullable=False, server_default=text("'[]'::jsonb")),
    Column("last_synced_at", DateTime(timezone=True)),
    Column("last_sync", JSONB, nullable=False, server_default=text("'{}'::jsonb")),
    Column("selected_by", Text, nullable=False),
    Column("selected_at", DateTime(timezone=True), nullable=False, server_default=func.now()),
    Column("updated_at", DateTime(timezone=True), nullable=False, server_default=func.now()),
    Column("version", Integer, nullable=False, server_default="1"),
    UniqueConstraint("tenant_id", "id", name="uq_project_confluence_sources_tenant_id"),
    UniqueConstraint("project_id", "connection_id", "space_id", name="uq_project_confluence_sources_space"),
    ForeignKeyConstraint(
        ["tenant_id", "project_id"],
        ["projects.tenant_id", "projects.id"],
        name="fk_confluence_sources_project",
    ),
    ForeignKeyConstraint(
        ["tenant_id", "connection_id"],
        ["source_connections.tenant_id", "source_connections.id"],
        name="fk_confluence_sources_connection",
    ),
    CheckConstraint("status IN ('ACTIVE','PAUSED')", name="ck_project_confluence_sources_status"),
    CheckConstraint(
        "pause_cause IN ('access','connection_revoked','excluded')",
        name="ck_project_confluence_sources_pause_cause",
    ),
)

confluence_pages = Table(
    "confluence_pages",
    metadata,
    Column("tenant_id", UUID(as_uuid=True), nullable=False),
    Column("source_id", UUID(as_uuid=True), nullable=False),
    Column("page_id", Text, nullable=False),
    Column("title", Text, nullable=False),
    Column("parent_page_id", Text),
    Column("ancestors", ARRAY(Text), nullable=False, server_default=text("'{}'::text[]")),
    Column("status", Text, nullable=False),
    Column("status_reason", Text),
    Column("current_version", Integer, nullable=False),
    Column("content_sha256", Text, nullable=False),
    Column("labels", ARRAY(Text), nullable=False, server_default=text("'{}'::text[]")),
    Column("restrictions", JSONB, nullable=False, server_default=text("'{}'::jsonb")),
    Column("attachments", JSONB, nullable=False, server_default=text("'[]'::jsonb")),
    Column("author", Text),
    Column("last_modified_at", Text),
    Column("web_url", Text),
    Column("first_seen_at", DateTime(timezone=True), nullable=False, server_default=func.now()),
    Column("updated_at", DateTime(timezone=True), nullable=False, server_default=func.now()),
    PrimaryKeyConstraint("source_id", "page_id", name="pk_confluence_pages"),
    ForeignKeyConstraint(
        ["tenant_id", "source_id"],
        ["project_confluence_sources.tenant_id", "project_confluence_sources.id"],
        name="fk_confluence_pages_source",
        ondelete="CASCADE",
    ),
    CheckConstraint(
        "status IN ('CURRENT','RESTRICTED','EXCLUDED','DELETED')", name="ck_confluence_pages_status"
    ),
)

confluence_page_versions = Table(
    "confluence_page_versions",
    metadata,
    Column("tenant_id", UUID(as_uuid=True), nullable=False),
    Column("source_id", UUID(as_uuid=True), nullable=False),
    Column("page_id", Text, nullable=False),
    Column("version", Integer, nullable=False),
    Column("content_sha256", Text, nullable=False),
    Column("title", Text, nullable=False),
    Column("author", Text),
    Column("created_at", Text),
    Column("message", Text),
    Column("seen_at", DateTime(timezone=True), nullable=False, server_default=func.now()),
    PrimaryKeyConstraint("source_id", "page_id", "version", name="pk_confluence_page_versions"),
    ForeignKeyConstraint(
        ["tenant_id", "source_id"],
        ["project_confluence_sources.tenant_id", "project_confluence_sources.id"],
        name="fk_confluence_versions_source",
        ondelete="CASCADE",
    ),
)

sync_jobs = Table(
    "sync_jobs",
    metadata,
    _id(),
    Column("tenant_id", UUID(as_uuid=True), ForeignKey("tenants.id"), nullable=False),
    Column("project_id", UUID(as_uuid=True)),
    Column("source_kind", Text, nullable=False),
    Column("source_id", UUID(as_uuid=True), nullable=False),
    Column("reason", Text, nullable=False),
    Column("mode", Text, nullable=False, server_default="incremental"),
    Column("status", Text, nullable=False, server_default="QUEUED"),
    Column("attempts", Integer, nullable=False, server_default="0"),
    Column("triggers", Integer, nullable=False, server_default="1"),
    Column("hints", JSONB, nullable=False, server_default=text("'{}'::jsonb")),
    Column("run_after", DateTime(timezone=True), nullable=False, server_default=func.now()),
    Column("locked_by", Text),
    Column("locked_until", DateTime(timezone=True)),
    Column("last_error", JSONB),
    Column("result", JSONB, nullable=False, server_default=text("'{}'::jsonb")),
    Column("created_at", DateTime(timezone=True), nullable=False, server_default=func.now()),
    Column("started_at", DateTime(timezone=True)),
    Column("finished_at", DateTime(timezone=True)),
    CheckConstraint(
        "source_kind IN ('git','jira','confluence','connection','migration','derive','purge','sweep','code',"
        "'embed')",
        name="ck_sync_jobs_kind",
    ),
    CheckConstraint("reason IN ('webhook','schedule','manual','recovery')", name="ck_sync_jobs_reason"),
    CheckConstraint("mode IN ('incremental','full')", name="ck_sync_jobs_mode"),
    CheckConstraint(
        "status IN ('QUEUED','RUNNING','SUCCEEDED','FAILED','DEAD','SKIPPED')", name="ck_sync_jobs_status"
    ),
)

source_sync_state = Table(
    "source_sync_state",
    metadata,
    Column("tenant_id", UUID(as_uuid=True), ForeignKey("tenants.id"), nullable=False),
    Column("project_id", UUID(as_uuid=True)),
    Column("source_kind", Text, nullable=False),
    Column("source_id", UUID(as_uuid=True), nullable=False),
    Column("watermark", JSONB, nullable=False, server_default=text("'{}'::jsonb")),
    Column("last_success_at", DateTime(timezone=True)),
    Column("last_attempt_at", DateTime(timezone=True)),
    Column("last_full_at", DateTime(timezone=True)),
    Column("next_due_at", DateTime(timezone=True)),
    Column("consecutive_failures", Integer, nullable=False, server_default="0"),
    Column("last_error", JSONB),
    Column("updated_at", DateTime(timezone=True), nullable=False, server_default=func.now()),
    PrimaryKeyConstraint("source_kind", "source_id", name="pk_source_sync_state"),
    CheckConstraint(
        "source_kind IN ('git','jira','confluence','connection','migration','derive','purge','sweep','code',"
        "'embed')",
        name="ck_source_sync_state_kind",
    ),
)

migration_plans = Table(
    "migration_plans",
    metadata,
    _id(),
    Column("tenant_id", UUID(as_uuid=True), nullable=False),
    Column("project_id", UUID(as_uuid=True), nullable=False),
    Column("jira_source_id", UUID(as_uuid=True), nullable=False),
    Column("import_mode", Text, nullable=False, server_default="jira_authoritative"),
    Column("history_since", Date),
    Column("status", Text, nullable=False, server_default="DRAFT"),
    Column("baseline", JSONB),
    Column("created_by", Text, nullable=False),
    Column("created_at", DateTime(timezone=True), nullable=False, server_default=func.now()),
    Column("updated_at", DateTime(timezone=True), nullable=False, server_default=func.now()),
    Column("version", Integer, nullable=False, server_default="1"),
    UniqueConstraint("project_id", name="uq_migration_plans_project"),
    UniqueConstraint("tenant_id", "id", name="uq_migration_plans_tenant_id"),
    ForeignKeyConstraint(
        ["tenant_id", "project_id"], ["projects.tenant_id", "projects.id"], name="fk_migration_plans_project"
    ),
    ForeignKeyConstraint(
        ["tenant_id", "jira_source_id"],
        ["project_jira_sources.tenant_id", "project_jira_sources.id"],
        name="fk_migration_plans_jira_source",
    ),
    CheckConstraint("import_mode IN ('jira_authoritative')", name="ck_migration_plans_mode"),
    CheckConstraint("status IN ('DRAFT','SCANNED','BASELINED')", name="ck_migration_plans_status"),
)

migration_runs = Table(
    "migration_runs",
    metadata,
    _id(),
    Column("tenant_id", UUID(as_uuid=True), nullable=False),
    Column("project_id", UUID(as_uuid=True), nullable=False),
    Column("plan_id", UUID(as_uuid=True), nullable=False),
    Column("plan_version", Integer, nullable=False, server_default="0"),
    Column("status", Text, nullable=False, server_default="QUEUED"),
    Column("cursor", Text),
    Column("pages_done", Integer, nullable=False, server_default="0"),
    Column("issues_seen", Integer, nullable=False, server_default="0"),
    Column("counts", JSONB, nullable=False, server_default=text("'{}'::jsonb")),
    Column("diagnostics", JSONB, nullable=False, server_default=text("'{}'::jsonb")),
    Column("scanned_as", JSONB, nullable=False, server_default=text("'{}'::jsonb")),
    Column("error", JSONB),
    Column("created_at", DateTime(timezone=True), nullable=False, server_default=func.now()),
    Column("started_at", DateTime(timezone=True)),
    Column("finished_at", DateTime(timezone=True)),
    UniqueConstraint("tenant_id", "id", name="uq_migration_runs_tenant_id"),
    ForeignKeyConstraint(
        ["tenant_id", "plan_id"],
        ["migration_plans.tenant_id", "migration_plans.id"],
        name="fk_migration_runs_plan",
    ),
    CheckConstraint("status IN ('QUEUED','RUNNING','SUCCEEDED','FAILED')", name="ck_migration_runs_status"),
)

migration_issues = Table(
    "migration_issues",
    metadata,
    Column("tenant_id", UUID(as_uuid=True), nullable=False),
    Column("project_id", UUID(as_uuid=True), nullable=False),
    Column("jira_issue_id", Text, nullable=False),
    Column("key", Text, nullable=False),
    Column("original_key", Text, nullable=False),
    Column("type_raw", Text, nullable=False),
    Column("type", Text, nullable=False),
    Column("status_raw", Text, nullable=False),
    Column("status", Text, nullable=False),
    Column("parent_issue_id", Text),
    Column("parent_key", Text),
    Column("summary", Text, nullable=False),
    Column("creator", Text),
    Column("reporter", Text),
    Column("created_at", Text),
    Column("updated_at", Text),
    Column("links", JSONB, nullable=False, server_default=text("'[]'::jsonb")),
    Column("flags", JSONB, nullable=False, server_default=text("'[]'::jsonb")),
    Column("present", Boolean, nullable=False, server_default=text("true")),
    Column("first_seen_run", UUID(as_uuid=True), nullable=False),
    Column("last_seen_run", UUID(as_uuid=True), nullable=False),
    Column("baseline_snapshot", JSONB),
    PrimaryKeyConstraint("project_id", "jira_issue_id", name="pk_migration_issues"),
    ForeignKeyConstraint(
        ["tenant_id", "project_id"], ["projects.tenant_id", "projects.id"], name="fk_migration_issues_project"
    ),
)

source_objects = Table(
    "source_objects",
    metadata,
    _id(),
    Column("tenant_id", UUID(as_uuid=True), nullable=False),
    Column("project_id", UUID(as_uuid=True), nullable=False),
    Column("source_kind", Text, nullable=False),
    Column("source_id", UUID(as_uuid=True), nullable=False),
    Column("external_id", Text, nullable=False),
    Column("parent_external_id", Text),
    Column("verified_ref", Text),
    Column("title", Text, nullable=False, server_default=""),
    Column("uri", Text),
    Column("status", Text, nullable=False, server_default="CURRENT"),
    Column("status_reason", Text),
    Column("acl", JSONB, nullable=False, server_default=text("'{}'::jsonb")),
    Column("current_revision_id", UUID(as_uuid=True)),
    Column("revisions", Integer, nullable=False, server_default="0"),
    Column("first_seen_at", DateTime(timezone=True), nullable=False, server_default=func.now()),
    Column("last_seen_at", DateTime(timezone=True), nullable=False, server_default=func.now()),
    Column("updated_at", DateTime(timezone=True), nullable=False, server_default=func.now()),
    Column("deleted_at", DateTime(timezone=True)),
    UniqueConstraint(
        "project_id", "source_kind", "source_id", "external_id", name="uq_source_objects_identity"
    ),
    UniqueConstraint("tenant_id", "id", name="uq_source_objects_tenant_id"),
    ForeignKeyConstraint(
        ["tenant_id", "project_id"], ["projects.tenant_id", "projects.id"], name="fk_source_objects_project"
    ),
    CheckConstraint("source_kind IN ('confluence','jira','git','upload')", name="ck_source_objects_kind"),
    CheckConstraint(
        "status IN ('CURRENT','RESTRICTED','EXCLUDED','DELETED','TOO_LARGE')", name="ck_source_objects_status"
    ),
)

source_revisions = Table(
    "source_revisions",
    metadata,
    _id(),
    Column("tenant_id", UUID(as_uuid=True), nullable=False),
    Column("object_id", UUID(as_uuid=True), nullable=False),
    Column("seq", Integer, nullable=False),
    Column("parent_revision_id", UUID(as_uuid=True)),
    Column("provider_version", Text),
    Column("content_sha256", Text, nullable=False),
    Column("size_bytes", BigInteger, nullable=False),
    Column("media_type", Text, nullable=False),
    Column("blob_key", Text),
    Column("acl", JSONB, nullable=False, server_default=text("'{}'::jsonb")),
    Column("job_id", UUID(as_uuid=True)),
    Column("fetched_at", DateTime(timezone=True), nullable=False, server_default=func.now()),
    Column("purged_at", DateTime(timezone=True)),
    UniqueConstraint("object_id", "seq", name="uq_source_revisions_seq"),
    UniqueConstraint("tenant_id", "id", name="uq_source_revisions_tenant_id"),
    ForeignKeyConstraint(
        ["tenant_id", "object_id"],
        ["source_objects.tenant_id", "source_objects.id"],
        name="fk_source_revisions_object",
        ondelete="CASCADE",
    ),
    CheckConstraint("seq >= 1", name="ck_source_revisions_seq"),
)

derivatives = Table(
    "derivatives",
    metadata,
    _id(),
    Column("tenant_id", UUID(as_uuid=True), nullable=False),
    Column("project_id", UUID(as_uuid=True), nullable=False),
    Column("revision_id", UUID(as_uuid=True), nullable=False),
    Column("kind", Text, nullable=False),
    Column("producer", Text, nullable=False),
    Column("producer_version", Integer, nullable=False),
    Column("status", Text, nullable=False, server_default="PENDING"),
    Column("output_sha256", Text),
    Column("size_bytes", BigInteger),
    Column("blob_key", Text),
    Column("error", JSONB),
    Column("reason", Text),
    Column("created_at", DateTime(timezone=True), nullable=False, server_default=func.now()),
    Column("built_at", DateTime(timezone=True)),
    Column("invalidated_at", DateTime(timezone=True)),
    Column("purged_at", DateTime(timezone=True)),
    ForeignKeyConstraint(
        ["tenant_id", "revision_id"],
        ["source_revisions.tenant_id", "source_revisions.id"],
        name="fk_derivatives_revision",
        ondelete="CASCADE",
    ),
    CheckConstraint(
        "status IN ('PENDING','READY','FAILED','SUPERSEDED','INVALIDATED')", name="ck_derivatives_status"
    ),
)

code_snapshots = Table(
    "code_snapshots",
    metadata,
    _id(),
    Column("tenant_id", UUID(as_uuid=True), nullable=False),
    Column("project_id", UUID(as_uuid=True), nullable=False),
    Column("source_id", UUID(as_uuid=True), nullable=False),
    Column("branch", Text, nullable=False),
    Column("commit_sha", Text, nullable=False),
    Column("status", Text, nullable=False, server_default="RUNNING"),
    Column("truncated", Boolean, nullable=False, server_default=text("false")),
    Column("counts", JSONB, nullable=False, server_default=text("'{}'::jsonb")),
    Column("skipped", JSONB, nullable=False, server_default=text("'[]'::jsonb")),
    Column("renames", JSONB, nullable=False, server_default=text("'[]'::jsonb")),
    Column("skipped_blobs", JSONB, nullable=False, server_default=text("'{}'::jsonb")),
    Column("codeowners", JSONB),
    Column("started_at", DateTime(timezone=True), nullable=False, server_default=func.now()),
    Column("finished_at", DateTime(timezone=True)),
    UniqueConstraint("source_id", "branch", "commit_sha", name="uq_code_snapshots_commit"),
    UniqueConstraint("tenant_id", "id", name="uq_code_snapshots_tenant_id"),
    ForeignKeyConstraint(
        ["tenant_id", "project_id"], ["projects.tenant_id", "projects.id"], name="fk_code_snapshots_project"
    ),
    CheckConstraint("status IN ('RUNNING','COMPLETE','PARTIAL','RETIRED')", name="ck_code_snapshots_status"),
)

code_symbols = Table(
    "code_symbols",
    metadata,
    Column("id", BigInteger, Identity(), primary_key=True),
    Column("tenant_id", UUID(as_uuid=True), nullable=False),
    Column("project_id", UUID(as_uuid=True), nullable=False),
    Column("derivative_id", UUID(as_uuid=True), nullable=False),
    Column("revision_id", UUID(as_uuid=True), nullable=False),
    Column("object_id", UUID(as_uuid=True), nullable=False),
    Column("source_id", UUID(as_uuid=True), nullable=False),
    Column("branch", Text, nullable=False),
    Column("path", Text, nullable=False),
    Column("blob_sha", Text, nullable=False),
    Column("commit_sha", Text, nullable=False, server_default=""),
    Column("language", Text, nullable=False),
    Column("kind", Text, nullable=False),
    Column("name", Text, nullable=False),
    Column("container", Text),
    Column("signature", Text),
    Column("line_start", Integer, nullable=False),
    Column("line_end", Integer, nullable=False),
    ForeignKeyConstraint(
        ["tenant_id", "project_id"], ["projects.tenant_id", "projects.id"], name="fk_code_symbols_project"
    ),
)

source_policies = Table(
    "source_policies",
    metadata,
    _id(),
    Column("tenant_id", UUID(as_uuid=True), nullable=False),
    Column("project_id", UUID(as_uuid=True), nullable=False),
    Column("source_kind", Text, nullable=False),
    Column("source_id", UUID(as_uuid=True), nullable=False),
    Column("trust", Text, nullable=False),
    Column("classification", Text, nullable=False),
    Column("reason", Text, nullable=False),
    Column("updated_by", Text, nullable=False),
    Column("updated_at", DateTime(timezone=True), nullable=False, server_default=func.now()),
    Column("version", Integer, nullable=False, server_default="1"),
    UniqueConstraint("source_kind", "source_id", name="uq_source_policies_source"),
    ForeignKeyConstraint(
        ["tenant_id", "project_id"], ["projects.tenant_id", "projects.id"], name="fk_source_policies_project"
    ),
    CheckConstraint("source_kind IN ('confluence','jira','git','upload')", name="ck_source_policies_kind"),
    CheckConstraint(
        "trust IN ('authoritative','trusted','reference','untrusted')", name="ck_source_policies_trust"
    ),
    CheckConstraint(
        "classification IN ('public','internal','confidential')", name="ck_source_policies_classification"
    ),
)

# INPROOM-186: the per-project knowledge change feed (written by knowledge_append(); see migration 0020).
knowledge_heads = Table(
    "knowledge_heads",
    metadata,
    Column("project_id", UUID(as_uuid=True), primary_key=True),
    Column("tenant_id", UUID(as_uuid=True), nullable=False),
    Column("seq", BigInteger, nullable=False, server_default="0"),
    Column("txid", BigInteger),
    ForeignKeyConstraint(
        ["tenant_id", "project_id"], ["projects.tenant_id", "projects.id"], name="fk_knowledge_heads_project"
    ),
)

knowledge_changes = Table(
    "knowledge_changes",
    metadata,
    Column("tenant_id", UUID(as_uuid=True), nullable=False),
    Column("project_id", UUID(as_uuid=True), nullable=False),
    Column("seq", BigInteger, nullable=False),
    Column("at", DateTime(timezone=True), nullable=False, server_default=func.now()),
    Column("change", Text, nullable=False),
    Column("scope", Text, nullable=False),
    Column("source_kind", Text, nullable=False),
    Column("source_id", UUID(as_uuid=True), nullable=False),
    Column("external_id", Text),
    Column("branch", Text),
    Column("reason", Text, nullable=False),
    Column("revision_seq", Integer),
    PrimaryKeyConstraint("project_id", "seq", name="pk_knowledge_changes"),
    ForeignKeyConstraint(
        ["tenant_id", "project_id"],
        ["projects.tenant_id", "projects.id"],
        name="fk_knowledge_changes_project",
    ),
    CheckConstraint("change IN ('invalidated','restored','refreshed')", name="ck_knowledge_changes_change"),
    CheckConstraint("scope IN ('item','source')", name="ck_knowledge_changes_scope"),
    CheckConstraint(
        "(scope = 'item') = (external_id IS NOT NULL)", name="ck_knowledge_changes_item_has_external_id"
    ),
)

# INPROOM-203: the queryable projection of READY chunks@1 derivatives (see migration 0022).
knowledge_chunks = Table(
    "knowledge_chunks",
    metadata,
    Column("id", BigInteger, Identity(), primary_key=True),
    Column("tenant_id", UUID(as_uuid=True), nullable=False),
    Column("project_id", UUID(as_uuid=True), nullable=False),
    Column("derivative_id", UUID(as_uuid=True), nullable=False),
    Column("revision_id", UUID(as_uuid=True), nullable=False),
    Column("object_id", UUID(as_uuid=True), nullable=False),
    Column("source_kind", Text, nullable=False),
    Column("source_id", UUID(as_uuid=True), nullable=False),
    Column("external_id", Text, nullable=False),
    Column("seq", Integer, nullable=False),
    Column("ordinal", Integer, nullable=False),
    Column("anchor", Text),
    Column("path", ARRAY(Text), nullable=False, server_default="{}"),
    Column("page", Integer),
    Column("line_start", Integer),
    Column("line_end", Integer),
    Column("context", Text, nullable=False, server_default=""),
    Column("text", Text, nullable=False),
    Column("tokens", Integer, nullable=False),
    Column("sha256", Text, nullable=False),
    Column("title", Text, nullable=False, server_default=""),  # the item's title when projected (INPROOM-211)
    # INPROOM-211: the lexical index (stemmed + verbatim; external id and context weighted A, text B)
    Column(
        "search_tsv",
        TSVECTOR,
        Computed(
            "setweight(to_tsvector('simple'::regconfig, title || ' ' || external_id || ' ' || context), 'A')"
            " || "
            "setweight(to_tsvector('english'::regconfig, title || ' ' || context), 'A') || "
            "setweight(to_tsvector('english'::regconfig, text), 'B') || "
            "setweight(to_tsvector('simple'::regconfig, text), 'B')",
            persisted=True,
        ),
    ),
    UniqueConstraint("derivative_id", "ordinal", name="uq_knowledge_chunks_ordinal"),
    ForeignKeyConstraint(
        ["tenant_id", "project_id"], ["projects.tenant_id", "projects.id"], name="fk_knowledge_chunks_project"
    ),
)
Index("ix_knowledge_chunks_revision", knowledge_chunks.c.project_id, knowledge_chunks.c.revision_id)
Index("ix_knowledge_chunks_sha", knowledge_chunks.c.tenant_id, knowledge_chunks.c.sha256)
Index("ix_knowledge_chunks_tsv", knowledge_chunks.c.search_tsv, postgresql_using="gin")

# INPROOM-204: one vector per (tenant, model, chunk sha256), and chunks a model permanently rejected.
chunk_embeddings = Table(
    "chunk_embeddings",
    metadata,
    Column("id", BigInteger, Identity(), primary_key=True),
    Column("tenant_id", UUID(as_uuid=True), nullable=False),
    Column("model", Text, nullable=False),
    Column("model_version", Text, nullable=False),
    Column("dimensions", Integer, nullable=False),
    Column("sha256", Text, nullable=False),
    Column("created_at", DateTime(timezone=True), nullable=False, server_default=func.now()),
    Column("embedding", Vector(), nullable=False),
    UniqueConstraint("tenant_id", "model", "sha256", name="uq_chunk_embeddings_key"),
)

chunk_embedding_failures = Table(
    "chunk_embedding_failures",
    metadata,
    Column("id", BigInteger, Identity(), primary_key=True),
    Column("tenant_id", UUID(as_uuid=True), nullable=False),
    Column("project_id", UUID(as_uuid=True), nullable=False),
    Column("model", Text, nullable=False),
    Column("sha256", Text, nullable=False),
    Column("code", Text, nullable=False),
    Column("detail", Text, nullable=False),
    Column("attempts", Integer, nullable=False, server_default="1"),
    Column("last_at", DateTime(timezone=True), nullable=False, server_default=func.now()),
    UniqueConstraint("project_id", "model", "sha256", name="uq_chunk_embedding_failures_key"),
    ForeignKeyConstraint(
        ["tenant_id", "project_id"],
        ["projects.tenant_id", "projects.id"],
        name="fk_chunk_embedding_failures_project",
    ),
)

Index("ix_audit_events_tenant_time", audit_events.c.tenant_id, audit_events.c.occurred_at)
Index("ix_outbox_unpublished", outbox_events.c.created_at, postgresql_where=text("published_at IS NULL"))
Index("ix_idempotency_created", idempotency_keys.c.created_at)
Index("ix_tenant_users_email", tenant_users.c.tenant_id, tenant_users.c.email)
Index(
    "uq_sync_jobs_queued",
    sync_jobs.c.source_kind,
    sync_jobs.c.source_id,
    unique=True,
    postgresql_where=text("status = 'QUEUED'"),
)
Index("ix_sync_jobs_due", sync_jobs.c.status, sync_jobs.c.run_after)
Index("ix_sync_jobs_project", sync_jobs.c.tenant_id, sync_jobs.c.project_id, sync_jobs.c.created_at)
Index(
    "ix_migration_runs_project",
    migration_runs.c.tenant_id,
    migration_runs.c.project_id,
    migration_runs.c.created_at,
)
Index("ix_migration_issues_parent", migration_issues.c.project_id, migration_issues.c.parent_issue_id)
Index(
    "ix_source_objects_source",
    source_objects.c.tenant_id,
    source_objects.c.project_id,
    source_objects.c.source_kind,
    source_objects.c.source_id,
)
Index("ix_source_revisions_sha", source_revisions.c.tenant_id, source_revisions.c.content_sha256)
Index(
    "uq_derivatives_live",
    derivatives.c.revision_id,
    derivatives.c.kind,
    derivatives.c.producer,
    derivatives.c.producer_version,
    unique=True,
    postgresql_where=text("status IN ('PENDING','READY','FAILED')"),
)
Index(
    "ix_derivatives_project_status", derivatives.c.tenant_id, derivatives.c.project_id, derivatives.c.status
)
Index("ix_derivatives_sha", derivatives.c.tenant_id, derivatives.c.output_sha256)
Index(
    "ix_source_objects_parent",
    source_objects.c.project_id,
    source_objects.c.source_kind,
    source_objects.c.source_id,
    source_objects.c.parent_external_id,
    postgresql_where=text("parent_external_id IS NOT NULL"),
)
Index(
    "ix_derivatives_producer_status",
    derivatives.c.tenant_id,
    derivatives.c.project_id,
    derivatives.c.producer,
    derivatives.c.status,
)
Index(
    "ix_code_snapshots_branch",
    code_snapshots.c.tenant_id,
    code_snapshots.c.source_id,
    code_snapshots.c.branch,
    code_snapshots.c.started_at,
)
Index(
    "ix_code_symbols_name",
    code_symbols.c.tenant_id,
    code_symbols.c.project_id,
    func.lower(code_symbols.c.name),
)
Index("ix_code_symbols_derivative", code_symbols.c.derivative_id)
