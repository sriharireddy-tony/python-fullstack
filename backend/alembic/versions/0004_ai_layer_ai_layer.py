"""Phase A: AI layer tables.

Revision ID: 0004_ai_layer
Revises: 0003_auth_lookup

Table creates came from autogenerate. Removed from that output: spurious ALTERs
against existing tables, caused by the naming convention renaming a check
constraint and by server-default comparison noise. This migration touches only
its own tables.

Added by hand, because autogenerate cannot produce them:
  * the enum types, created explicitly with checkfirst
  * row-level-security policies on every tenant-scoped table
  * the append-only grant on ai_usage
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0004_ai_layer"
down_revision: str | None = "0003_auth_lookup"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


#: Tables holding tenant data. Each gets RLS enabled, FORCED, and a policy.
#: The AI layer introduces a new leak surface; it gets no exemption.
TENANT_SCOPED = (
    "ticket_vector_state",
    "ai_suggestions",
    "ai_analysis_runs",
    "ai_analysis_steps",
    "ai_eval_pairs",
    "ai_conversations",
)

#: Deliberately NOT scoped, with the reason recorded so nobody "fixes" them:
#:   ai_jobs  - the worker runs with no tenant context and reads the scope FROM
#:              the row, so a policy would make jobs unclaimable.
#:   ai_usage - tenant_id is nullable for platform-level calls; scoped in the
#:              query layer instead.


def _enum(name: str, *values: str) -> postgresql.ENUM:
    """Enum type reference.

    ``create_type=False`` because each type is created explicitly below with
    checkfirst. Left at the default, ``create_table`` would emit a second
    CREATE TYPE for the same name and fail.
    """
    return postgresql.ENUM(*values, name=name, create_type=False)


def _enable_rls(table: str) -> None:
    """Enable RLS and add the tenant policy.

    ``current_setting('app.tenant_id', true)`` uses missing_ok, so an unset
    scope yields NULL and matches no rows -- failing closed. FORCE matters: the
    table owner is otherwise exempt, and the owner is the role that runs
    migrations.
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


ENUMS: list[tuple[str, tuple[str, ...]]] = [
    ("ai_job_kind", ("embed_ticket", "delete_vector", "analyse_ticket")),
    ("ai_job_status", ("queued", "running", "done", "failed")),
    ("ai_suggestion_kind", ("similar", "triage", "resolution")),
    ("ai_suggestion_status", ("pending", "accepted", "rejected", "expired")),
    (
        "ai_analysis_status",
        ("queued", "running", "completed", "failed", "budget_exceeded", "timed_out"),
    ),
    ("ai_relation", ("duplicate", "related", "recurring", "unrelated")),
]


def upgrade() -> None:
    bind = op.get_bind()
    for name, values in ENUMS:
        _enum(name, *values).create(bind, checkfirst=True)

    op.create_table(
        "ai_jobs",
        sa.Column("tenant_id", sa.UUID(), nullable=False),
        sa.Column(
            "kind",
            _enum("ai_job_kind", "embed_ticket", "delete_vector", "analyse_ticket"),
            nullable=False,
        ),
        sa.Column(
            "status",
            _enum("ai_job_status", "queued", "running", "done", "failed"),
            nullable=False,
        ),
        sa.Column(
            "payload",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
        sa.Column("attempts", sa.Integer(), nullable=False),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.Column(
            "scheduled_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("locked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("locked_by", sa.String(length=120), nullable=True),
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_ai_jobs")),
    )
    op.create_index("ix_ai_jobs_claim", "ai_jobs", ["status", "scheduled_at"], unique=False)
    op.create_index("ix_ai_jobs_tenant", "ai_jobs", ["tenant_id"], unique=False)
    op.create_table(
        "ai_usage",
        sa.Column("tenant_id", sa.UUID(), nullable=True),
        sa.Column("feature", sa.String(length=60), nullable=False),
        sa.Column("role", sa.String(length=60), nullable=False),
        sa.Column("model", sa.String(length=120), nullable=False),
        sa.Column("input_tokens", sa.Integer(), nullable=False),
        sa.Column("output_tokens", sa.Integer(), nullable=False),
        sa.Column("cached_tokens", sa.Integer(), nullable=False),
        sa.Column("estimated_cost", sa.Float(), nullable=False),
        sa.Column("latency_ms", sa.Integer(), nullable=True),
        sa.Column("succeeded", sa.Boolean(), server_default=sa.text("true"), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("id", sa.UUID(), nullable=False),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_ai_usage")),
    )
    op.create_index("ix_ai_usage_model_created", "ai_usage", ["model", "created_at"], unique=False)
    op.create_index(
        "ix_ai_usage_tenant_created", "ai_usage", ["tenant_id", "created_at"], unique=False
    )
    op.create_table(
        "ai_conversations",
        sa.Column("user_id", sa.UUID(), nullable=False),
        sa.Column("thread_id", sa.String(length=64), nullable=False),
        sa.Column("title", sa.String(length=300), nullable=True),
        sa.Column("message_count", sa.Integer(), nullable=False),
        sa.Column("last_message_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("tenant_id", sa.UUID(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(
            ["tenant_id"],
            ["tenants.id"],
            name=op.f("fk_ai_conversations_tenant_id_tenants"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["users.id"],
            name=op.f("fk_ai_conversations_user_id_users"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_ai_conversations")),
        sa.UniqueConstraint("thread_id", name=op.f("uq_ai_conversations_thread_id")),
    )
    op.create_index(
        op.f("ix_ai_conversations_deleted_at"), "ai_conversations", ["deleted_at"], unique=False
    )
    op.create_index(
        op.f("ix_ai_conversations_tenant_id"), "ai_conversations", ["tenant_id"], unique=False
    )
    op.create_index("ix_conversations_expiry", "ai_conversations", ["expires_at"], unique=False)
    op.create_index(
        "ix_conversations_user", "ai_conversations", ["user_id", "created_at"], unique=False
    )
    op.create_table(
        "ai_analysis_runs",
        sa.Column("ticket_id", sa.UUID(), nullable=False),
        sa.Column("requested_by_id", sa.UUID(), nullable=True),
        sa.Column(
            "status",
            _enum(
                "ai_analysis_status",
                "queued",
                "running",
                "completed",
                "failed",
                "budget_exceeded",
                "timed_out",
            ),
            nullable=False,
        ),
        sa.Column("report", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("partial", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.Column("model", sa.String(length=120), nullable=True),
        sa.Column(
            "used_fallback_model", sa.Boolean(), server_default=sa.text("false"), nullable=False
        ),
        sa.Column("turns", sa.Integer(), nullable=False),
        sa.Column("tool_calls", sa.Integer(), nullable=False),
        sa.Column("input_tokens", sa.Integer(), nullable=False),
        sa.Column("output_tokens", sa.Integer(), nullable=False),
        sa.Column("estimated_cost", sa.Float(), nullable=False),
        sa.Column("latency_ms", sa.Integer(), nullable=True),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("helpful", sa.Boolean(), nullable=True),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("tenant_id", sa.UUID(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["requested_by_id"],
            ["users.id"],
            name=op.f("fk_ai_analysis_runs_requested_by_id_users"),
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id"],
            ["tenants.id"],
            name=op.f("fk_ai_analysis_runs_tenant_id_tenants"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["ticket_id"],
            ["tickets.id"],
            name=op.f("fk_ai_analysis_runs_ticket_id_tickets"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_ai_analysis_runs")),
    )
    op.create_index(
        op.f("ix_ai_analysis_runs_tenant_id"), "ai_analysis_runs", ["tenant_id"], unique=False
    )
    op.create_index(
        "ix_ai_runs_tenant_status", "ai_analysis_runs", ["tenant_id", "status"], unique=False
    )
    op.create_index(
        "ix_ai_runs_ticket", "ai_analysis_runs", ["ticket_id", "created_at"], unique=False
    )
    op.create_table(
        "ai_eval_pairs",
        sa.Column("ticket_a_id", sa.UUID(), nullable=False),
        sa.Column("ticket_b_id", sa.UUID(), nullable=False),
        sa.Column(
            "relation",
            _enum("ai_relation", "duplicate", "related", "recurring", "unrelated"),
            nullable=False,
        ),
        sa.Column("source", sa.String(length=40), nullable=False),
        sa.Column("labelled_by_id", sa.UUID(), nullable=True),
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("tenant_id", sa.UUID(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["labelled_by_id"],
            ["users.id"],
            name=op.f("fk_ai_eval_pairs_labelled_by_id_users"),
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id"],
            ["tenants.id"],
            name=op.f("fk_ai_eval_pairs_tenant_id_tenants"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["ticket_a_id"],
            ["tickets.id"],
            name=op.f("fk_ai_eval_pairs_ticket_a_id_tickets"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["ticket_b_id"],
            ["tickets.id"],
            name=op.f("fk_ai_eval_pairs_ticket_b_id_tickets"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_ai_eval_pairs")),
    )
    op.create_index(
        op.f("ix_ai_eval_pairs_tenant_id"), "ai_eval_pairs", ["tenant_id"], unique=False
    )
    op.create_index("ix_eval_tenant", "ai_eval_pairs", ["tenant_id"], unique=False)
    op.create_index("uq_eval_pair", "ai_eval_pairs", ["ticket_a_id", "ticket_b_id"], unique=True)
    op.create_table(
        "ai_suggestions",
        sa.Column("ticket_id", sa.UUID(), nullable=False),
        sa.Column(
            "kind",
            _enum("ai_suggestion_kind", "similar", "triage", "resolution"),
            nullable=False,
        ),
        sa.Column("related_ticket_id", sa.UUID(), nullable=True),
        sa.Column(
            "relation",
            _enum("ai_relation", "duplicate", "related", "recurring", "unrelated"),
            nullable=True,
        ),
        sa.Column("confidence", sa.Float(), nullable=True),
        sa.Column("reason", sa.Text(), nullable=True),
        sa.Column(
            "payload",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
        sa.Column("model", sa.String(length=120), nullable=True),
        sa.Column(
            "status",
            _enum("ai_suggestion_status", "pending", "accepted", "rejected", "expired"),
            nullable=False,
        ),
        sa.Column("decided_by_id", sa.UUID(), nullable=True),
        sa.Column("decided_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("tenant_id", sa.UUID(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["decided_by_id"],
            ["users.id"],
            name=op.f("fk_ai_suggestions_decided_by_id_users"),
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["related_ticket_id"],
            ["tickets.id"],
            name=op.f("fk_ai_suggestions_related_ticket_id_tickets"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id"],
            ["tenants.id"],
            name=op.f("fk_ai_suggestions_tenant_id_tenants"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["ticket_id"],
            ["tickets.id"],
            name=op.f("fk_ai_suggestions_ticket_id_tickets"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_ai_suggestions")),
    )
    op.create_index(
        "ix_ai_suggestions_tenant", "ai_suggestions", ["tenant_id", "created_at"], unique=False
    )
    op.create_index(
        op.f("ix_ai_suggestions_tenant_id"), "ai_suggestions", ["tenant_id"], unique=False
    )
    op.create_index(
        "ix_ai_suggestions_ticket", "ai_suggestions", ["ticket_id", "kind", "status"], unique=False
    )
    op.create_table(
        "ticket_vector_state",
        sa.Column("ticket_id", sa.UUID(), nullable=False),
        sa.Column("model", sa.String(length=120), nullable=False),
        sa.Column("dimension", sa.Integer(), nullable=False),
        sa.Column("source_hash", sa.String(length=64), nullable=False),
        sa.Column("synced_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("tenant_id", sa.UUID(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id"],
            ["tenants.id"],
            name=op.f("fk_ticket_vector_state_tenant_id_tenants"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["ticket_id"],
            ["tickets.id"],
            name=op.f("fk_ticket_vector_state_ticket_id_tickets"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_ticket_vector_state")),
    )
    op.create_index(
        op.f("ix_ticket_vector_state_tenant_id"), "ticket_vector_state", ["tenant_id"], unique=False
    )
    op.create_index("ix_tvs_tenant", "ticket_vector_state", ["tenant_id"], unique=False)
    op.create_index(
        "uq_tvs_ticket_model", "ticket_vector_state", ["ticket_id", "model"], unique=True
    )
    op.create_table(
        "ai_analysis_steps",
        sa.Column("run_id", sa.UUID(), nullable=False),
        sa.Column("turn", sa.Integer(), nullable=False),
        sa.Column("tool_name", sa.String(length=120), nullable=True),
        sa.Column("tool_args", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("result_summary", sa.Text(), nullable=True),
        sa.Column("input_tokens", sa.Integer(), nullable=False),
        sa.Column("output_tokens", sa.Integer(), nullable=False),
        sa.Column("latency_ms", sa.Integer(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("tenant_id", sa.UUID(), nullable=False),
        sa.ForeignKeyConstraint(
            ["run_id"],
            ["ai_analysis_runs.id"],
            name=op.f("fk_ai_analysis_steps_run_id_ai_analysis_runs"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id"],
            ["tenants.id"],
            name=op.f("fk_ai_analysis_steps_tenant_id_tenants"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_ai_analysis_steps")),
    )
    op.create_index(
        op.f("ix_ai_analysis_steps_tenant_id"), "ai_analysis_steps", ["tenant_id"], unique=False
    )
    op.create_index("ix_ai_steps_run", "ai_analysis_steps", ["run_id", "turn"], unique=False)
    for table in TENANT_SCOPED:
        _enable_rls(table)

    # ai_usage is append-only for the application role: cost records are
    # evidence, and evidence that can be edited is not evidence. Wrapped
    # because os_app may not exist in a single-role setup.
    op.execute(
        """
        DO $$
        BEGIN
            IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'os_app') THEN
                REVOKE UPDATE, DELETE ON ai_usage FROM os_app;
                REVOKE UPDATE, DELETE ON ai_analysis_steps FROM os_app;
            END IF;
        END
        $$
        """
    )


def downgrade() -> None:
    for table in TENANT_SCOPED:
        op.execute(f"DROP POLICY IF EXISTS tenant_isolation ON {table}")

    op.drop_index("ix_ai_steps_run", table_name="ai_analysis_steps")
    op.drop_index(op.f("ix_ai_analysis_steps_tenant_id"), table_name="ai_analysis_steps")
    op.drop_table("ai_analysis_steps")
    op.drop_index("uq_tvs_ticket_model", table_name="ticket_vector_state")
    op.drop_index("ix_tvs_tenant", table_name="ticket_vector_state")
    op.drop_index(op.f("ix_ticket_vector_state_tenant_id"), table_name="ticket_vector_state")
    op.drop_table("ticket_vector_state")
    op.drop_index("ix_ai_suggestions_ticket", table_name="ai_suggestions")
    op.drop_index(op.f("ix_ai_suggestions_tenant_id"), table_name="ai_suggestions")
    op.drop_index("ix_ai_suggestions_tenant", table_name="ai_suggestions")
    op.drop_table("ai_suggestions")
    op.drop_index("uq_eval_pair", table_name="ai_eval_pairs")
    op.drop_index("ix_eval_tenant", table_name="ai_eval_pairs")
    op.drop_index(op.f("ix_ai_eval_pairs_tenant_id"), table_name="ai_eval_pairs")
    op.drop_table("ai_eval_pairs")
    op.drop_index("ix_ai_runs_ticket", table_name="ai_analysis_runs")
    op.drop_index("ix_ai_runs_tenant_status", table_name="ai_analysis_runs")
    op.drop_index(op.f("ix_ai_analysis_runs_tenant_id"), table_name="ai_analysis_runs")
    op.drop_table("ai_analysis_runs")
    op.drop_index("ix_conversations_user", table_name="ai_conversations")
    op.drop_index("ix_conversations_expiry", table_name="ai_conversations")
    op.drop_index(op.f("ix_ai_conversations_tenant_id"), table_name="ai_conversations")
    op.drop_index(op.f("ix_ai_conversations_deleted_at"), table_name="ai_conversations")
    op.drop_table("ai_conversations")
    op.drop_index("ix_ai_usage_tenant_created", table_name="ai_usage")
    op.drop_index("ix_ai_usage_model_created", table_name="ai_usage")
    op.drop_table("ai_usage")
    op.drop_index("ix_ai_jobs_tenant", table_name="ai_jobs")
    op.drop_index("ix_ai_jobs_claim", table_name="ai_jobs")
    op.drop_table("ai_jobs")

    for name, _ in ENUMS:
        op.execute(f"DROP TYPE IF EXISTS {name}")
