"""SQLAlchemy Core table definitions. Migrations are hand-written in migrations/versions; keep both in sync.

Every tenant-scoped table carries tenant_id and is protected by FORCE ROW LEVEL SECURITY (see 0001 migration).
"""

from sqlalchemy import (
    CheckConstraint,
    Column,
    DateTime,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    Integer,
    MetaData,
    PrimaryKeyConstraint,
    SmallInteger,
    Table,
    Text,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import ARRAY, JSONB, UUID

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


Index("ix_audit_events_tenant_time", audit_events.c.tenant_id, audit_events.c.occurred_at)
Index("ix_outbox_unpublished", outbox_events.c.created_at, postgresql_where=text("published_at IS NULL"))
Index("ix_idempotency_created", idempotency_keys.c.created_at)
Index("ix_tenant_users_email", tenant_users.c.tenant_id, tenant_users.c.email)
