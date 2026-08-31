"""Ticket and status history."""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import (
    Boolean,
    Computed,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    text,
)
from sqlalchemy.dialects.postgresql import TSVECTOR
from sqlalchemy.dialects.postgresql import UUID as PGUUID
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base
from app.core.models import (
    SoftDeleteMixin,
    TenantScopedMixin,
    TimestampMixin,
    UUIDPrimaryKeyMixin,
    VersionedMixin,
    pg_enum,
)
from app.modules.tickets.enums import (
    Environment,
    Impact,
    Priority,
    RejectionReason,
    Severity,
    TicketStatus,
    WaitingOn,
    Workaround,
)


class Ticket(
    Base,
    UUIDPrimaryKeyMixin,
    TenantScopedMixin,
    TimestampMixin,
    SoftDeleteMixin,
    VersionedMixin,
):
    __tablename__ = "tickets"
    __table_args__ = (
        # Every composite index leads with tenant_id -- not decoration. RLS
        # injects that predicate into every query, so an index without it
        # cannot be used efficiently.
        Index(
            "ix_tickets_tenant_status_team",
            "tenant_id",
            "status",
            "team_id",
            "created_at",
            postgresql_where=text("deleted_at IS NULL"),
        ),
        Index(
            "ix_tickets_tenant_assignee",
            "tenant_id",
            "assignee_id",
            "status",
            postgresql_where=text("deleted_at IS NULL"),
        ),
        Index(
            "ix_tickets_tenant_client",
            "tenant_id",
            "client_id",
            "created_at",
            postgresql_where=text("deleted_at IS NULL"),
        ),
        Index(
            "ix_tickets_tenant_priority",
            "tenant_id",
            "priority",
            "status",
            postgresql_where=text("deleted_at IS NULL"),
        ),
        Index("uq_tickets_tenant_number", "tenant_id", "ticket_number", unique=True),
        Index("ix_tickets_search", "search_vector", postgresql_using="gin"),
    )

    #: Per-tenant sequence, displayed as ``OS-1042``. Allocated from
    #: ``tenant_counters`` with a row lock -- never MAX()+1, which races.
    ticket_number: Mapped[int] = mapped_column(Integer, nullable=False)

    title: Mapped[str] = mapped_column(String(300), nullable=False)
    description: Mapped[str] = mapped_column(Text, nullable=False)
    steps_to_reproduce: Mapped[str | None] = mapped_column(Text)
    expected_result: Mapped[str | None] = mapped_column(Text)
    actual_result: Mapped[str | None] = mapped_column(Text)

    environment: Mapped[Environment] = mapped_column(
        pg_enum(Environment, "ticket_environment"), nullable=False
    )

    # --- who reported it -------------------------------------------------
    client_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("clients.id", ondelete="RESTRICT"), nullable=False
    )
    reporter_name: Mapped[str | None] = mapped_column(String(200))
    reporter_email: Mapped[str | None] = mapped_column(String(320))
    created_by_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("users.id", ondelete="RESTRICT"), nullable=False
    )

    # --- who owns it -----------------------------------------------------
    team_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("teams.id", ondelete="RESTRICT"), nullable=False
    )
    #: Null means the ticket sits unassigned in the team inbox.
    assignee_id: Mapped[uuid.UUID | None] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL")
    )

    # --- urgency ---------------------------------------------------------
    severity: Mapped[Severity] = mapped_column(pg_enum(Severity, "ticket_severity"), nullable=False)
    impact: Mapped[Impact] = mapped_column(pg_enum(Impact, "ticket_impact"), nullable=False)
    workaround: Mapped[Workaround] = mapped_column(
        pg_enum(Workaround, "ticket_workaround"), nullable=False
    )
    #: Derived from the three fields above, then stored -- so it can be indexed
    #: and filtered, and so an override is simply a different value here.
    priority: Mapped[Priority] = mapped_column(pg_enum(Priority, "ticket_priority"), nullable=False)
    priority_overridden: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default=text("false")
    )
    priority_override_reason: Mapped[str | None] = mapped_column(Text)
    priority_overridden_by_id: Mapped[uuid.UUID | None] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL")
    )

    # --- lifecycle -------------------------------------------------------
    status: Mapped[TicketStatus] = mapped_column(
        pg_enum(TicketStatus, "ticket_status"),
        nullable=False,
        default=TicketStatus.OPEN,
    )
    on_hold_waiting_on: Mapped[WaitingOn | None] = mapped_column(
        pg_enum(WaitingOn, "ticket_waiting_on")
    )
    resolution_notes: Mapped[str | None] = mapped_column(Text)
    rejection_reason: Mapped[RejectionReason | None] = mapped_column(
        pg_enum(RejectionReason, "ticket_rejection_reason")
    )

    #: Optional and at the developer's discretion. OS Tracker is the system of
    #: record; ADO is downstream and not synchronised in v1.
    ado_work_item_url: Mapped[str | None] = mapped_column(Text)

    #: Deliberately unused in v1. A nullable column costs nothing now;
    #: retrofitting it onto thousands of tickets leaves them permanently blank.
    duplicate_of_id: Mapped[uuid.UUID | None] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("tickets.id", ondelete="SET NULL")
    )

    #: Quality signal: a high reopen rate means fixes are not being verified.
    reopen_count: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default=text("0")
    )

    resolved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    closed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    #: Generated column maintained by Postgres, so it can never drift from the
    #: text it indexes the way a trigger or application-side update can.
    #:
    #: Declared as Computed so SQLAlchemy leaves it out of INSERT and UPDATE --
    #: Postgres rejects any write to a GENERATED ALWAYS column. The expression
    #: mirrors migration 0002; the database is the source of truth for it.
    search_vector: Mapped[str | None] = mapped_column(
        TSVECTOR,
        Computed(
            "setweight(to_tsvector('english', coalesce(title, '')), 'A') || "
            "setweight(to_tsvector('english', coalesce(description, '')), 'B') || "
            "setweight(to_tsvector('english', coalesce(steps_to_reproduce, '')), 'C')",
            persisted=True,
        ),
        nullable=True,
    )

    @property
    def reference(self) -> str:
        return f"OS-{self.ticket_number}"

    def __repr__(self) -> str:
        return f"<Ticket {self.reference} {self.status}>"


class TicketStatusHistory(Base, UUIDPrimaryKeyMixin):
    """One row per transition.

    This is the hedge that keeps SLA, MTTR, aging, and reopen-rate reporting
    possible later -- **including for tickets created before those features
    exist**. That data cannot be reconstructed if it was never captured.

    Kept separate from ``audit_logs`` on purpose: audit is generic with a JSONB
    payload, which is fine for "who changed what" and miserable for "average
    time from assigned to resolved, grouped by team".
    """

    __tablename__ = "ticket_status_history"
    __table_args__ = (
        Index("ix_ticket_status_history_ticket", "ticket_id", "changed_at"),
        Index("ix_ticket_status_history_tenant", "tenant_id", "changed_at"),
    )

    tenant_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("tenants.id", ondelete="RESTRICT"), nullable=False
    )
    ticket_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("tickets.id", ondelete="CASCADE"), nullable=False
    )

    from_status: Mapped[TicketStatus | None] = mapped_column(pg_enum(TicketStatus, "ticket_status"))
    to_status: Mapped[TicketStatus] = mapped_column(
        pg_enum(TicketStatus, "ticket_status"),
        nullable=False,
    )

    changed_by_id: Mapped[uuid.UUID | None] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL")
    )
    note: Mapped[str | None] = mapped_column(Text)
    changed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()")
    )
