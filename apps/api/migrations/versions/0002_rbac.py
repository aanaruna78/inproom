"""RBAC (INPROOM-20): tenant user directory and a closed set of project roles.

Revision ID: 0002_rbac
Revises: 0001_tenancy_projects
Create Date: 2026-11-23

- tenant_users: who has signed in to the tenant (registered on GET /v1/me). Project members can only be
  added from this directory, so a user of another tenant can never be granted access. Forced RLS +
  tenant policy like every other tenant-scoped table.
- project_members.role is limited to the 9 project roles (tenant roles never live in this table).
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import UUID

revision = "0002_rbac"
down_revision = "0001_tenancy_projects"
branch_labels = None
depends_on = None

APP_ROLE = "inproom_app"  # frozen literal - do not import app code into migrations
PROJECT_ROLES = (
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


def upgrade() -> None:
    op.create_table(
        "tenant_users",
        sa.Column("tenant_id", UUID(as_uuid=True), sa.ForeignKey("tenants.id"), nullable=False),
        sa.Column("user_id", sa.Text, nullable=False),
        sa.Column("email", sa.Text),
        sa.Column("display_name", sa.Text),
        sa.Column("first_seen_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("last_seen_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.PrimaryKeyConstraint("tenant_id", "user_id", name="pk_tenant_users"),
    )
    op.create_index("ix_tenant_users_email", "tenant_users", ["tenant_id", "email"])
    op.execute(f"GRANT SELECT, INSERT, UPDATE ON tenant_users TO {APP_ROLE}")
    op.execute("ALTER TABLE tenant_users ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE tenant_users FORCE ROW LEVEL SECURITY")
    op.execute(
        "CREATE POLICY tenant_isolation ON tenant_users "
        "USING (tenant_id = current_tenant()) WITH CHECK (tenant_id = current_tenant())"
    )
    roles = ",".join(f"'{r}'" for r in PROJECT_ROLES)
    op.create_check_constraint("ck_project_members_role", "project_members", f"role IN ({roles})")


def downgrade() -> None:
    op.drop_constraint("ck_project_members_role", "project_members", type_="check")
    op.execute(f"REVOKE ALL ON tenant_users FROM {APP_ROLE}")
    op.drop_index("ix_tenant_users_email", table_name="tenant_users")
    op.drop_table("tenant_users")
