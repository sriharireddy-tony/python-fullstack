"""Phase 1: tenancy, identity, teams, and row-level security.

Revision ID: 0001_identity
Revises:
Create Date: 2026-08-31

"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0001_identity"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


# Tables holding tenant data. Each gets RLS enabled plus a policy scoping every
# statement to the tenant in the session variable set by the application.
TENANT_SCOPED_TABLES = ("users", "teams")


def _enable_rls(table: str) -> None:
    """Enable row-level security and add the tenant policy.

    ``current_setting('app.tenant_id', true)`` uses missing_ok, so an unset
    scope yields NULL and the comparison matches no rows. Failing closed is the
    correct default: code that forgets to set the tenant sees nothing rather
    than everything.

    FORCE is important -- without it the table owner is exempt, and if the
    application ever connected as the owner every policy here would be
    decorative.
    """
    op.execute(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY")
    op.execute(f"ALTER TABLE {table} FORCE ROW LEVEL SECURITY")
    op.execute(
        f"""
        CREATE POLICY tenant_isolation ON {table}
        USING (tenant_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid)
        WITH CHECK (tenant_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid)
        """
    )


def upgrade() -> None:
    op.execute('CREATE EXTENSION IF NOT EXISTS "citext"')

    user_role = postgresql.ENUM(
        "tenant_admin",
        "cs_lead",
        "cs_agent",
        "team_manager",
        "developer",
        name="user_role",
        # Created explicitly below. Without create_type=False,
        # create_table would emit a second CREATE TYPE and fail.
        create_type=False,
    )
    user_role.create(op.get_bind(), checkfirst=True)

    # ------------------------------------------------------------ tenants
    op.create_table(
        "tenants",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("name", sa.String(200), nullable=False),
        sa.Column("code", sa.String(50), nullable=False),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint("id", name="pk_tenants"),
        sa.UniqueConstraint("code", name="uq_tenants_code"),
    )
    op.create_index("ix_tenants_deleted_at", "tenants", ["deleted_at"])

    # ---------------------------------------------------- platform_admins
    op.create_table(
        "platform_admins",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("email", postgresql.CITEXT(), nullable=False),
        sa.Column("password_hash", sa.String(255), nullable=False),
        sa.Column("full_name", sa.String(200), nullable=False),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("last_login_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint("id", name="pk_platform_admins"),
        sa.UniqueConstraint("email", name="uq_platform_admins_email"),
    )
    op.create_index("ix_platform_admins_deleted_at", "platform_admins", ["deleted_at"])

    # ---------------------------------------------------- tenant_counters
    op.create_table(
        "tenant_counters",
        sa.Column("tenant_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("last_ticket_number", sa.Integer(), nullable=False, server_default="0"),
        sa.ForeignKeyConstraint(
            ["tenant_id"],
            ["tenants.id"],
            name="fk_tenant_counters_tenant_id_tenants",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("tenant_id", name="pk_tenant_counters"),
    )

    # -------------------------------------------------------------- users
    op.create_table(
        "users",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("tenant_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("email", postgresql.CITEXT(), nullable=False),
        sa.Column("password_hash", sa.String(255), nullable=False),
        sa.Column("full_name", sa.String(200), nullable=False),
        sa.Column("role", user_role, nullable=False),
        sa.Column(
            "preferences", postgresql.JSONB(), nullable=False, server_default=sa.text("'{}'::jsonb")
        ),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("last_login_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(
            ["tenant_id"], ["tenants.id"], name="fk_users_tenant_id_tenants", ondelete="RESTRICT"
        ),
        sa.PrimaryKeyConstraint("id", name="pk_users"),
    )
    op.create_index("ix_users_tenant_id", "users", ["tenant_id"])
    op.create_index("ix_users_deleted_at", "users", ["deleted_at"])
    op.create_index("ix_users_tenant_active", "users", ["tenant_id", "is_active"])
    op.execute(
        "CREATE UNIQUE INDEX uq_users_email_lower ON users (lower(email)) WHERE deleted_at IS NULL"
    )

    # -------------------------------------------------------------- teams
    op.create_table(
        "teams",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("tenant_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("name", sa.String(120), nullable=False),
        sa.Column("code", sa.String(50), nullable=False),
        sa.Column("description", sa.String(500), nullable=True),
        sa.Column("manager_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(
            ["tenant_id"], ["tenants.id"], name="fk_teams_tenant_id_tenants", ondelete="RESTRICT"
        ),
        sa.ForeignKeyConstraint(
            ["manager_id"], ["users.id"], name="fk_teams_manager_id_users", ondelete="SET NULL"
        ),
        sa.PrimaryKeyConstraint("id", name="pk_teams"),
    )
    op.create_index("ix_teams_tenant_id", "teams", ["tenant_id"])
    op.create_index("ix_teams_deleted_at", "teams", ["deleted_at"])
    op.execute(
        "CREATE UNIQUE INDEX uq_teams_tenant_name_lower ON teams (tenant_id, lower(name)) "
        "WHERE deleted_at IS NULL"
    )
    op.execute(
        "CREATE UNIQUE INDEX uq_teams_tenant_code_lower ON teams (tenant_id, lower(code)) "
        "WHERE deleted_at IS NULL"
    )

    # --------------------------------------------------------- user_teams
    op.create_table(
        "user_teams",
        sa.Column("user_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("team_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("is_primary", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.ForeignKeyConstraint(
            ["user_id"], ["users.id"], name="fk_user_teams_user_id_users", ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["team_id"], ["teams.id"], name="fk_user_teams_team_id_teams", ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("user_id", "team_id", name="pk_user_teams"),
    )

    # ----------------------------------------------------- refresh_tokens
    op.create_table(
        "refresh_tokens",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("user_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("token_hash", sa.String(64), nullable=False),
        sa.Column("family_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column(
            "issued_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False
        ),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("replaced_by_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("user_agent", sa.String(400), nullable=True),
        sa.Column("ip_address", sa.String(64), nullable=True),
        sa.ForeignKeyConstraint(
            ["user_id"], ["users.id"], name="fk_refresh_tokens_user_id_users", ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id", name="pk_refresh_tokens"),
        sa.UniqueConstraint("token_hash", name="uq_refresh_tokens_token_hash"),
    )
    op.create_index("ix_refresh_tokens_user_active", "refresh_tokens", ["user_id", "revoked_at"])
    op.create_index("ix_refresh_tokens_family", "refresh_tokens", ["family_id"])

    # ---------------------------------------------------------------- RLS
    for table in TENANT_SCOPED_TABLES:
        _enable_rls(table)


def downgrade() -> None:
    for table in TENANT_SCOPED_TABLES:
        op.execute(f"DROP POLICY IF EXISTS tenant_isolation ON {table}")

    op.drop_table("refresh_tokens")
    op.drop_table("user_teams")
    op.drop_table("teams")
    op.drop_table("users")
    op.drop_table("tenant_counters")
    op.drop_table("platform_admins")
    op.drop_table("tenants")
    op.execute("DROP TYPE IF EXISTS user_role")
