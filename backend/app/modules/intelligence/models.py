"""AI layer tables.

Note what is absent: **no table stores ticket text.** Pinecone holds vectors, ids,
and filter metadata; Postgres holds the tickets. Results are hydrated from
Postgres, so sensitive content lives in exactly one place — one store to back
up, secure, and satisfy a deletion request against.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from enum import StrEnum
from typing import Any

from sqlalchemy import (
    Boolean,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PGUUID
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base
from app.core.models import (
    SoftDeleteMixin,
    TenantScopedMixin,
    TimestampMixin,
    UUIDPrimaryKeyMixin,
    pg_enum,
)
from app.modules.intelligence.schemas.types import Relation


class AiJobKind(StrEnum):
    EMBED_TICKET = "embed_ticket"
    DELETE_VECTOR = "delete_vector"
    ANALYSE_TICKET = "analyse_ticket"


class AiJobStatus(StrEnum):
    QUEUED = "queued"
    RUNNING = "running"
    DONE = "done"
    FAILED = "failed"


class SuggestionKind(StrEnum):
    SIMILAR = "similar"
    TRIAGE = "triage"
    RESOLUTION = "resolution"


class SuggestionStatus(StrEnum):
    PENDING = "pending"
    ACCEPTED = "accepted"
    REJECTED = "rejected"
    EXPIRED = "expired"


class AnalysisStatus(StrEnum):
    QUEUED = "queued"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    BUDGET_EXCEEDED = "budget_exceeded"
    TIMED_OUT = "timed_out"


#: `Relation` is imported from `domain/types.py` rather than defined here, so
#: the domain layer stays free of SQLAlchemy while the column that stores it
#: still has the enum. The dependency points models -> domain, which is the
#: direction it should.
__all__ = ["Relation"]


class TicketVectorState(Base, UUIDPrimaryKeyMixin, TenantScopedMixin, TimestampMixin):
    """Bookkeeping for what has been embedded.

    This table is what makes reconciliation possible. Without it, answering
    "which tickets are missing vectors?" means reading every id out of Pinecone
    and diffing — neither transactional nor scalable.

    ``source_hash`` covers the model name, the instruction version, and the
    normalised text, so changing any of them forces a re-embed rather than
    leaving a corpus half in each convention.
    """

    __tablename__ = "ticket_vector_state"
    __table_args__ = (
        Index("uq_tvs_ticket_model", "ticket_id", "model", unique=True),
        Index("ix_tvs_tenant", "tenant_id"),
    )

    ticket_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("tickets.id", ondelete="CASCADE"), nullable=False
    )
    model: Mapped[str] = mapped_column(String(120), nullable=False)
    dimension: Mapped[int] = mapped_column(Integer, nullable=False)
    source_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    synced_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class AiJob(Base, UUIDPrimaryKeyMixin, TimestampMixin):
    """The transactional outbox.

    Written in the same transaction as the change that triggers it, so the
    job's existence is atomic with the ticket's. Enqueueing to an external
    broker instead would be a dual write: roll back after the push and you have
    a job for a ticket that does not exist; push after commit fails and the
    ticket is never embedded with nothing recording the miss.

    Not tenant-scoped by RLS on purpose — the worker runs with no tenant
    context and reads the scope *from* the row.
    """

    __tablename__ = "ai_jobs"
    __table_args__ = (
        Index("ix_ai_jobs_claim", "status", "scheduled_at"),
        Index("ix_ai_jobs_tenant", "tenant_id"),
    )

    tenant_id: Mapped[uuid.UUID] = mapped_column(PGUUID(as_uuid=True), nullable=False)
    kind: Mapped[AiJobKind] = mapped_column(pg_enum(AiJobKind, "ai_job_kind"), nullable=False)
    status: Mapped[AiJobStatus] = mapped_column(
        pg_enum(AiJobStatus, "ai_job_status"), nullable=False, default=AiJobStatus.QUEUED
    )
    payload: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, server_default=text("'{}'::jsonb")
    )
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    last_error: Mapped[str | None] = mapped_column(Text)
    scheduled_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()")
    )
    locked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    locked_by: Mapped[str | None] = mapped_column(String(120))


class AiSuggestion(Base, UUIDPrimaryKeyMixin, TenantScopedMixin, TimestampMixin):
    """A suggestion, stored separately from the ticket's real fields.

    A suggested duplicate link never touches ``tickets.duplicate_of_id`` until
    someone accepts it. ``status`` plus ``decided_by_id`` is also the accuracy
    signal — and every accept is a labelled positive pair for the eval set, so
    the feature and the data collection are the same thing.
    """

    __tablename__ = "ai_suggestions"
    __table_args__ = (
        Index("ix_ai_suggestions_ticket", "ticket_id", "kind", "status"),
        Index("ix_ai_suggestions_tenant", "tenant_id", "created_at"),
    )

    ticket_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("tickets.id", ondelete="CASCADE"), nullable=False
    )
    kind: Mapped[SuggestionKind] = mapped_column(
        pg_enum(SuggestionKind, "ai_suggestion_kind"), nullable=False
    )
    related_ticket_id: Mapped[uuid.UUID | None] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("tickets.id", ondelete="CASCADE")
    )
    relation: Mapped[Relation | None] = mapped_column(pg_enum(Relation, "ai_relation"))
    confidence: Mapped[float | None] = mapped_column(Float)
    reason: Mapped[str | None] = mapped_column(Text)
    payload: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, server_default=text("'{}'::jsonb")
    )
    model: Mapped[str | None] = mapped_column(String(120))
    status: Mapped[SuggestionStatus] = mapped_column(
        pg_enum(SuggestionStatus, "ai_suggestion_status"),
        nullable=False,
        default=SuggestionStatus.PENDING,
    )
    decided_by_id: Mapped[uuid.UUID | None] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL")
    )
    decided_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class AiAnalysisRun(Base, UUIDPrimaryKeyMixin, TenantScopedMixin, TimestampMixin):
    """One agent run."""

    __tablename__ = "ai_analysis_runs"
    __table_args__ = (
        Index("ix_ai_runs_ticket", "ticket_id", "created_at"),
        Index("ix_ai_runs_tenant_status", "tenant_id", "status"),
    )

    ticket_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("tickets.id", ondelete="CASCADE"), nullable=False
    )
    requested_by_id: Mapped[uuid.UUID | None] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL")
    )
    status: Mapped[AnalysisStatus] = mapped_column(
        pg_enum(AnalysisStatus, "ai_analysis_status"),
        nullable=False,
        default=AnalysisStatus.QUEUED,
    )
    report: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    #: True when a limit stopped the run. A truncated analysis that says what it
    #: skipped is more useful than a lost one.
    partial: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default=text("false")
    )
    model: Mapped[str | None] = mapped_column(String(120))
    used_fallback_model: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default=text("false")
    )
    turns: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    tool_calls: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    input_tokens: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    output_tokens: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    estimated_cost: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    latency_ms: Mapped[int | None] = mapped_column(Integer)
    error: Mapped[str | None] = mapped_column(Text)
    #: Thumbs up/down. The only signal that says whether analyses are worth
    #: their cost.
    helpful: Mapped[bool | None] = mapped_column(Boolean)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class AiAnalysisStep(Base, UUIDPrimaryKeyMixin, TenantScopedMixin):
    """One agent turn.

    Without this, "why did it miss the obvious duplicate?" is unanswerable.
    With it you can see that it searched once with a poor query and never
    retried — or that it called the same tool five times with near-identical
    arguments.
    """

    __tablename__ = "ai_analysis_steps"
    __table_args__ = (Index("ix_ai_steps_run", "run_id", "turn"),)

    run_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey("ai_analysis_runs.id", ondelete="CASCADE"),
        nullable=False,
    )
    turn: Mapped[int] = mapped_column(Integer, nullable=False)
    tool_name: Mapped[str | None] = mapped_column(String(120))
    tool_args: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    result_summary: Mapped[str | None] = mapped_column(Text)
    input_tokens: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    output_tokens: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    latency_ms: Mapped[int | None] = mapped_column(Integer)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()")
    )


class AiUsage(Base, UUIDPrimaryKeyMixin):
    """Per-call token and cost attribution.

    Turns "the AI features cost too much" from an argument into a query, and is
    the basis for the per-tenant budget and for cost per *accepted* suggestion
    — which is the number worth optimising, unlike cost per call.
    """

    __tablename__ = "ai_usage"
    __table_args__ = (
        Index("ix_ai_usage_tenant_created", "tenant_id", "created_at"),
        Index("ix_ai_usage_model_created", "model", "created_at"),
    )

    tenant_id: Mapped[uuid.UUID | None] = mapped_column(PGUUID(as_uuid=True))
    feature: Mapped[str] = mapped_column(String(60), nullable=False)
    role: Mapped[str] = mapped_column(String(60), nullable=False)
    model: Mapped[str] = mapped_column(String(120), nullable=False)
    input_tokens: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    output_tokens: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    cached_tokens: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    estimated_cost: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    latency_ms: Mapped[int | None] = mapped_column(Integer)
    succeeded: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True, server_default=text("true")
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()")
    )


class AiEvalPair(Base, UUIDPrimaryKeyMixin, TenantScopedMixin, TimestampMixin):
    """The golden set.

    Seeded from the synthetic generator's planted duplicate clusters, then
    grown for free from real accept/reject decisions.
    """

    __tablename__ = "ai_eval_pairs"
    __table_args__ = (
        Index("uq_eval_pair", "ticket_a_id", "ticket_b_id", unique=True),
        Index("ix_eval_tenant", "tenant_id"),
    )

    ticket_a_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("tickets.id", ondelete="CASCADE"), nullable=False
    )
    ticket_b_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("tickets.id", ondelete="CASCADE"), nullable=False
    )
    relation: Mapped[Relation] = mapped_column(pg_enum(Relation, "ai_relation"), nullable=False)
    #: 'planted' | 'derived_from_link' | 'human'
    source: Mapped[str] = mapped_column(String(40), nullable=False)
    labelled_by_id: Mapped[uuid.UUID | None] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL")
    )


class Conversation(Base, UUIDPrimaryKeyMixin, TenantScopedMixin, TimestampMixin, SoftDeleteMixin):
    """A chat thread.

    Private to its creator — not readable by managers or admins. That is what
    makes the bot useful: if people knew their queries were readable they would
    stop asking the half-formed questions a conversational interface is for.

    ``thread_id`` is the LangGraph checkpointer key. Expired monthly, because
    the checkpoint tables grow quietly and hold ticket content.
    """

    __tablename__ = "ai_conversations"
    __table_args__ = (
        Index("ix_conversations_user", "user_id", "created_at"),
        Index("ix_conversations_expiry", "expires_at"),
    )

    user_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    thread_id: Mapped[str] = mapped_column(String(64), nullable=False, unique=True)
    title: Mapped[str | None] = mapped_column(String(300))
    message_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    last_message_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
