"""Phase 2-5: clients, tickets, history, comments, attachments, audit.

Revision ID: 0002_tickets
Revises: 0001_identity
Create Date: 2026-08-31

"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0002_tickets"
down_revision: str | None = "0001_identity"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


TENANT_SCOPED_TABLES = (
    "clients",
    "tickets",
    "ticket_status_history",
    "comments",
    "attachments",
)


def _enable_rls(table: str) -> None:
    """Enable RLS and add the tenant policy.

    ``current_setting('app.tenant_id', true)`` uses missing_ok, so an unset
    scope yields NULL and matches no rows -- failing closed. FORCE matters: the
    table owner is otherwise exempt, which would make every policy decorative.
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


def _enum(name: str, *values: str) -> postgresql.ENUM:
    """Enum type reference.

    ``create_type=False`` because each type is created explicitly with
    ``checkfirst=True`` before the tables. Left at the default, ``create_table``
    would emit a second CREATE TYPE for the same name and fail.
    """
    return postgresql.ENUM(*values, name=name, create_type=False)


def upgrade() -> None:
    bind = op.get_bind()

    client_tier = _enum("client_tier", "standard", "premium", "enterprise")
    severity = _enum("ticket_severity", "s1_critical", "s2_major", "s3_minor", "s4_cosmetic")
    impact = _enum("ticket_impact", "whole_org", "department", "few_users", "single_user")
    workaround = _enum("ticket_workaround", "none", "painful", "easy")
    priority = _enum("ticket_priority", "p1", "p2", "p3", "p4")
    ticket_status = _enum(
        "ticket_status",
        "open",
        "assigned",
        "in_progress",
        "on_hold",
        "resolved",
        "rejected",
        "closed",
    )
    environment = _enum("ticket_environment", "production", "staging", "uat")
    rejection = _enum(
        "ticket_rejection_reason", "not_a_bug", "duplicate", "wont_fix", "cannot_reproduce"
    )
    waiting_on = _enum("ticket_waiting_on", "cs", "client", "other_team")
    actor_type = _enum("audit_actor_type", "user", "platform_admin", "system")

    for enum_type in (
        client_tier,
        severity,
        impact,
        workaround,
        priority,
        ticket_status,
        environment,
        rejection,
        waiting_on,
        actor_type,
    ):
        enum_type.create(bind, checkfirst=True)

    ts = lambda: sa.Column(  # noqa: E731
        "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
    )

    # ------------------------------------------------------------ clients
    op.create_table(
        "clients",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("tenant_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("name", sa.String(200), nullable=False),
        sa.Column("code", sa.String(50), nullable=False),
        sa.Column("tier", client_tier, nullable=False, server_default="standard"),
        sa.Column("notes", sa.Text(), nullable=True),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.true()),
        ts(),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(
            ["tenant_id"], ["tenants.id"], name="fk_clients_tenant_id_tenants", ondelete="RESTRICT"
        ),
        sa.PrimaryKeyConstraint("id", name="pk_clients"),
    )
    op.create_index("ix_clients_tenant_id", "clients", ["tenant_id"])
    op.create_index("ix_clients_deleted_at", "clients", ["deleted_at"])
    op.create_index("ix_clients_tenant_active", "clients", ["tenant_id", "is_active"])
    op.execute(
        "CREATE UNIQUE INDEX uq_clients_tenant_name_lower ON clients (tenant_id, lower(name)) "
        "WHERE deleted_at IS NULL"
    )
    op.execute(
        "CREATE UNIQUE INDEX uq_clients_tenant_code_lower ON clients (tenant_id, lower(code)) "
        "WHERE deleted_at IS NULL"
    )

    # ------------------------------------------------------------ tickets
    op.create_table(
        "tickets",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("tenant_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("ticket_number", sa.Integer(), nullable=False),
        sa.Column("title", sa.String(300), nullable=False),
        sa.Column("description", sa.Text(), nullable=False),
        sa.Column("steps_to_reproduce", sa.Text(), nullable=True),
        sa.Column("expected_result", sa.Text(), nullable=True),
        sa.Column("actual_result", sa.Text(), nullable=True),
        sa.Column("environment", environment, nullable=False),
        sa.Column("client_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("reporter_name", sa.String(200), nullable=True),
        sa.Column("reporter_email", sa.String(320), nullable=True),
        sa.Column("created_by_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("team_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("assignee_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("severity", severity, nullable=False),
        sa.Column("impact", impact, nullable=False),
        sa.Column("workaround", workaround, nullable=False),
        sa.Column("priority", priority, nullable=False),
        sa.Column("priority_overridden", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("priority_override_reason", sa.Text(), nullable=True),
        sa.Column("priority_overridden_by_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("status", ticket_status, nullable=False, server_default="open"),
        sa.Column("on_hold_waiting_on", waiting_on, nullable=True),
        sa.Column("resolution_notes", sa.Text(), nullable=True),
        sa.Column("rejection_reason", rejection, nullable=True),
        sa.Column("ado_work_item_url", sa.Text(), nullable=True),
        sa.Column("duplicate_of_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("reopen_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("resolved_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("closed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("version", sa.Integer(), nullable=False, server_default="1"),
        ts(),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(
            ["tenant_id"], ["tenants.id"], name="fk_tickets_tenant_id_tenants", ondelete="RESTRICT"
        ),
        sa.ForeignKeyConstraint(
            ["client_id"], ["clients.id"], name="fk_tickets_client_id_clients", ondelete="RESTRICT"
        ),
        sa.ForeignKeyConstraint(
            ["team_id"], ["teams.id"], name="fk_tickets_team_id_teams", ondelete="RESTRICT"
        ),
        sa.ForeignKeyConstraint(
            ["created_by_id"],
            ["users.id"],
            name="fk_tickets_created_by_id_users",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["assignee_id"], ["users.id"], name="fk_tickets_assignee_id_users", ondelete="SET NULL"
        ),
        sa.ForeignKeyConstraint(
            ["priority_overridden_by_id"],
            ["users.id"],
            name="fk_tickets_priority_overridden_by_id_users",
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["duplicate_of_id"],
            ["tickets.id"],
            name="fk_tickets_duplicate_of_id_tickets",
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_tickets"),
    )

    # Generated column: maintained by Postgres, so it can never drift from the
    # text it indexes the way a trigger or application-side update can.
    op.execute(
        """
        ALTER TABLE tickets ADD COLUMN search_vector tsvector
        GENERATED ALWAYS AS (
            setweight(to_tsvector('english', coalesce(title, '')), 'A') ||
            setweight(to_tsvector('english', coalesce(description, '')), 'B') ||
            setweight(to_tsvector('english', coalesce(steps_to_reproduce, '')), 'C')
        ) STORED
        """
    )

    op.create_index("ix_tickets_tenant_id", "tickets", ["tenant_id"])
    op.create_index("ix_tickets_deleted_at", "tickets", ["deleted_at"])
    op.execute("CREATE UNIQUE INDEX uq_tickets_tenant_number ON tickets (tenant_id, ticket_number)")
    op.execute(
        "CREATE INDEX ix_tickets_tenant_status_team ON tickets "
        "(tenant_id, status, team_id, created_at) WHERE deleted_at IS NULL"
    )
    op.execute(
        "CREATE INDEX ix_tickets_tenant_assignee ON tickets "
        "(tenant_id, assignee_id, status) WHERE deleted_at IS NULL"
    )
    op.execute(
        "CREATE INDEX ix_tickets_tenant_client ON tickets "
        "(tenant_id, client_id, created_at) WHERE deleted_at IS NULL"
    )
    op.execute(
        "CREATE INDEX ix_tickets_tenant_priority ON tickets "
        "(tenant_id, priority, status) WHERE deleted_at IS NULL"
    )
    op.execute("CREATE INDEX ix_tickets_search ON tickets USING gin (search_vector)")

    # ------------------------------------------- ticket_status_history
    op.create_table(
        "ticket_status_history",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("tenant_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("ticket_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("from_status", ticket_status, nullable=True),
        sa.Column("to_status", ticket_status, nullable=False),
        sa.Column("changed_by_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("note", sa.Text(), nullable=True),
        sa.Column(
            "changed_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id"], ["tenants.id"], name="fk_tsh_tenant_id_tenants", ondelete="RESTRICT"
        ),
        sa.ForeignKeyConstraint(
            ["ticket_id"], ["tickets.id"], name="fk_tsh_ticket_id_tickets", ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["changed_by_id"], ["users.id"], name="fk_tsh_changed_by_id_users", ondelete="SET NULL"
        ),
        sa.PrimaryKeyConstraint("id", name="pk_ticket_status_history"),
    )
    op.create_index(
        "ix_ticket_status_history_ticket", "ticket_status_history", ["ticket_id", "changed_at"]
    )
    op.create_index(
        "ix_ticket_status_history_tenant", "ticket_status_history", ["tenant_id", "changed_at"]
    )

    # ----------------------------------------------------------- comments
    op.create_table(
        "comments",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("tenant_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("ticket_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("author_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("body", sa.Text(), nullable=False),
        sa.Column("edited_at", sa.DateTime(timezone=True), nullable=True),
        ts(),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(
            ["tenant_id"], ["tenants.id"], name="fk_comments_tenant_id_tenants", ondelete="RESTRICT"
        ),
        sa.ForeignKeyConstraint(
            ["ticket_id"], ["tickets.id"], name="fk_comments_ticket_id_tickets", ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["author_id"], ["users.id"], name="fk_comments_author_id_users", ondelete="RESTRICT"
        ),
        sa.PrimaryKeyConstraint("id", name="pk_comments"),
    )
    op.create_index("ix_comments_tenant_id", "comments", ["tenant_id"])
    op.create_index("ix_comments_deleted_at", "comments", ["deleted_at"])
    op.execute(
        "CREATE INDEX ix_comments_ticket_created ON comments (ticket_id, created_at) "
        "WHERE deleted_at IS NULL"
    )

    # -------------------------------------------------------- attachments
    op.create_table(
        "attachments",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("tenant_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("ticket_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("comment_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("uploaded_by_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("original_filename", sa.String(255), nullable=False),
        sa.Column("storage_key", sa.String(500), nullable=False),
        sa.Column("content_type", sa.String(120), nullable=False),
        sa.Column("size_bytes", sa.BigInteger(), nullable=False),
        sa.Column("checksum", sa.String(64), nullable=False),
        ts(),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(
            ["tenant_id"],
            ["tenants.id"],
            name="fk_attachments_tenant_id_tenants",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["ticket_id"],
            ["tickets.id"],
            name="fk_attachments_ticket_id_tickets",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["comment_id"],
            ["comments.id"],
            name="fk_attachments_comment_id_comments",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["uploaded_by_id"],
            ["users.id"],
            name="fk_attachments_uploaded_by_id_users",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_attachments"),
        sa.UniqueConstraint("storage_key", name="uq_attachments_storage_key"),
        # Exactly one parent, enforced in the database so no code path can
        # create an orphan or a double-parented row.
        sa.CheckConstraint(
            "(ticket_id IS NOT NULL AND comment_id IS NULL) "
            "OR (ticket_id IS NULL AND comment_id IS NOT NULL)",
            name="ck_attachments_exactly_one_parent",
        ),
    )
    op.create_index("ix_attachments_tenant_id", "attachments", ["tenant_id"])
    op.create_index("ix_attachments_deleted_at", "attachments", ["deleted_at"])
    op.execute(
        "CREATE INDEX ix_attachments_ticket ON attachments (ticket_id) WHERE deleted_at IS NULL"
    )
    op.execute(
        "CREATE INDEX ix_attachments_comment ON attachments (comment_id) WHERE deleted_at IS NULL"
    )

    # --------------------------------------------------------- audit_logs
    op.create_table(
        "audit_logs",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("tenant_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("actor_type", actor_type, nullable=False),
        sa.Column("actor_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("action", sa.String(100), nullable=False),
        sa.Column("entity_type", sa.String(50), nullable=False),
        sa.Column("entity_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column(
            "changes", postgresql.JSONB(), nullable=False, server_default=sa.text("'{}'::jsonb")
        ),
        sa.Column("ip_address", sa.String(64), nullable=True),
        sa.Column("user_agent", sa.String(400), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id", name="pk_audit_logs"),
    )
    op.create_index(
        "ix_audit_logs_tenant_entity",
        "audit_logs",
        ["tenant_id", "entity_type", "entity_id", "created_at"],
    )
    op.create_index("ix_audit_logs_tenant_created", "audit_logs", ["tenant_id", "created_at"])
    op.create_index("ix_audit_logs_actor", "audit_logs", ["actor_id", "created_at"])

    # audit_logs is append-only. Revoking UPDATE and DELETE from the
    # application role makes that structural rather than a convention someone
    # can forget. Wrapped because os_app may not exist in a single-role setup.
    op.execute(
        """
        DO $$
        BEGIN
            IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'os_app') THEN
                REVOKE UPDATE, DELETE ON audit_logs FROM os_app;
            END IF;
        END
        $$
        """
    )

    for table in TENANT_SCOPED_TABLES:
        _enable_rls(table)


def downgrade() -> None:
    for table in TENANT_SCOPED_TABLES:
        op.execute(f"DROP POLICY IF EXISTS tenant_isolation ON {table}")

    op.drop_table("audit_logs")
    op.drop_table("attachments")
    op.drop_table("comments")
    op.drop_table("ticket_status_history")
    op.drop_table("tickets")
    op.drop_table("clients")

    for enum_name in (
        "audit_actor_type",
        "ticket_waiting_on",
        "ticket_rejection_reason",
        "ticket_environment",
        "ticket_status",
        "ticket_priority",
        "ticket_workaround",
        "ticket_impact",
        "ticket_severity",
        "client_tier",
    ):
        op.execute(f"DROP TYPE IF EXISTS {enum_name}")
