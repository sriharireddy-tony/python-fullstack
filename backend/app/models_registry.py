"""Single import point for every ORM model.

Alembic autogenerate only sees models that have been imported. Re-exporting
them here means adding a module is one line in one place, rather than scattered
imports in ``alembic/env.py``.

Modules still never import each other's models directly -- this file exists for
metadata registration only, and nothing in the application imports it.
"""

from __future__ import annotations

from app.modules.attachments.models import Attachment
from app.modules.audit.models import AuditLog
from app.modules.clients.models import Client
from app.modules.comments.models import Comment
from app.modules.identity.models import RefreshToken, User, UserTeam
from app.modules.intelligence.models import (
    AiAnalysisRun,
    AiAnalysisStep,
    AiEvalPair,
    AiJob,
    AiSuggestion,
    AiUsage,
    Conversation,
    TicketVectorState,
)
from app.modules.teams.models import Team
from app.modules.tenancy.models import PlatformAdmin, Tenant, TenantCounter
from app.modules.tickets.models import Ticket, TicketStatusHistory

__all__ = [
    "AiAnalysisRun",
    "AiAnalysisStep",
    "AiEvalPair",
    "AiJob",
    "AiSuggestion",
    "AiUsage",
    "Attachment",
    "AuditLog",
    "Client",
    "Comment",
    "Conversation",
    "PlatformAdmin",
    "RefreshToken",
    "Team",
    "Tenant",
    "TenantCounter",
    "Ticket",
    "TicketStatusHistory",
    "TicketVectorState",
    "User",
    "UserTeam",
]

#: Tables holding tenant data. Each MUST have both a ``tenant_id`` column and a
#: row-level-security policy, created in the same migration.
#:
#: New tables get added under deadline pressure and a missing policy is silent,
#: so ``check_rls_coverage()`` asserts the two stay in step and is wired into
#: the quality gates.
TENANT_SCOPED_TABLES: frozenset[str] = frozenset(
    {
        "ticket_vector_state",
        "ai_suggestions",
        "ai_analysis_runs",
        "ai_analysis_steps",
        "ai_eval_pairs",
        "ai_conversations",
        "users",
        "teams",
        "clients",
        "tickets",
        "ticket_status_history",
        "comments",
        "attachments",
    }
)

#: Deliberately NOT tenant-scoped, with the reason recorded so a future reader
#: does not "fix" them:
#:
#:   tenants          - the tenant registry itself
#:   platform_admins  - sit above all tenants by design
#:   tenant_counters  - keyed by tenant_id as its primary key, admin-internal
#:   refresh_tokens   - keyed by user; only reachable via a verified token
#:   user_teams       - a join table between two already-scoped tables
#:   audit_logs       - tenant_id is nullable, because platform-admin actions
#:                      belong to no tenant; scoped in the query layer instead
UNSCOPED_TABLES: frozenset[str] = frozenset(
    {
        "tenants",
        "platform_admins",
        "tenant_counters",
        "refresh_tokens",
        "user_teams",
        "audit_logs",
        # ai_jobs: the worker runs with no tenant context and reads the scope
        # FROM the row, so RLS would make it unclaimable.
        "ai_jobs",
        # ai_usage: tenant_id is nullable for platform-level calls; scoped in
        # the query layer instead.
        "ai_usage",
        "alembic_version",
    }
)


def check_rls_coverage() -> list[str]:
    """Verify every mapped table is classified, and scoped tables carry tenant_id.

    Returns a list of problems; empty means the model layer is consistent.
    Whether the *policies* actually exist in the database is a separate check
    that needs a live connection -- see ``scripts/check_rls.py``.
    """
    from app.core.database import Base

    problems: list[str] = []
    classified = TENANT_SCOPED_TABLES | UNSCOPED_TABLES

    for name, table in Base.metadata.tables.items():
        if name not in classified:
            problems.append(f"{name}: not listed as tenant-scoped or unscoped in models_registry")
            continue
        if name in TENANT_SCOPED_TABLES and "tenant_id" not in table.columns:
            problems.append(f"{name}: listed as tenant-scoped but has no tenant_id column")

    for name in TENANT_SCOPED_TABLES:
        if name not in Base.metadata.tables:
            problems.append(f"{name}: listed as tenant-scoped but no such table is mapped")

    return problems
