"""Append-only audit log.

Distinct from application logs: those are operational, sampled, and disposable.
This is a permanent business record, queryable in SQL and exposed in the UI.
The first time someone disputes a reassignment or a priority override, this is
what settles it.

Never updated, never deleted -- enforced by grants, not just convention.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from enum import StrEnum
from typing import Any

from sqlalchemy import DateTime, Index, String, text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PGUUID
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base
from app.core.models import UUIDPrimaryKeyMixin, pg_enum


class ActorType(StrEnum):
    USER = "user"
    PLATFORM_ADMIN = "platform_admin"
    SYSTEM = "system"


class AuditAction(StrEnum):
    """Recorded actions.

    A closed set rather than free text, so reports and filters stay reliable.
    """

    TICKET_CREATED = "ticket.created"
    TICKET_UPDATED = "ticket.updated"
    TICKET_ASSIGNED = "ticket.assigned"
    TICKET_TRANSITIONED = "ticket.transitioned"
    TICKET_RESOLVED = "ticket.resolved"
    TICKET_REJECTED = "ticket.rejected"
    TICKET_CLOSED = "ticket.closed"
    TICKET_REOPENED = "ticket.reopened"
    TICKET_TEAM_TRANSFERRED = "ticket.team_transferred"
    TICKET_PRIORITY_OVERRIDDEN = "ticket.priority_overridden"

    COMMENT_CREATED = "comment.created"
    COMMENT_DELETED = "comment.deleted"
    ATTACHMENT_UPLOADED = "attachment.uploaded"
    ATTACHMENT_DELETED = "attachment.deleted"
    ATTACHMENT_DOWNLOADED = "attachment.downloaded"

    USER_CREATED = "user.created"
    USER_UPDATED = "user.updated"
    USER_DEACTIVATED = "user.deactivated"
    USER_REACTIVATED = "user.reactivated"
    USER_TICKETS_REASSIGNED = "user.tickets_reassigned"

    TEAM_CREATED = "team.created"
    TEAM_UPDATED = "team.updated"
    CLIENT_CREATED = "client.created"
    CLIENT_UPDATED = "client.updated"

    TENANT_CREATED = "tenant.created"
    TENANT_UPDATED = "tenant.updated"


class AuditLog(Base, UUIDPrimaryKeyMixin):
    __tablename__ = "audit_logs"
    __table_args__ = (
        Index("ix_audit_logs_tenant_entity", "tenant_id", "entity_type", "entity_id", "created_at"),
        Index("ix_audit_logs_tenant_created", "tenant_id", "created_at"),
        Index("ix_audit_logs_actor", "actor_id", "created_at"),
    )

    # Nullable: platform-admin actions belong to no tenant.
    tenant_id: Mapped[uuid.UUID | None] = mapped_column(PGUUID(as_uuid=True), nullable=True)

    actor_type: Mapped[ActorType] = mapped_column(
        pg_enum(ActorType, "audit_actor_type"), nullable=False
    )
    actor_id: Mapped[uuid.UUID | None] = mapped_column(PGUUID(as_uuid=True), nullable=True)

    action: Mapped[str] = mapped_column(String(100), nullable=False)
    entity_type: Mapped[str] = mapped_column(String(50), nullable=False)
    entity_id: Mapped[uuid.UUID | None] = mapped_column(PGUUID(as_uuid=True), nullable=True)

    # Before/after values. Sensitive content is redacted by the service before
    # it reaches here -- see AuditService.
    changes: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, server_default=text("'{}'::jsonb")
    )

    ip_address: Mapped[str | None] = mapped_column(String(64))
    user_agent: Mapped[str | None] = mapped_column(String(400))

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()")
    )
