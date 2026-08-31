"""Ticket conversation.

All comments are internal -- there is no client-facing view. This is where a
rejection gets explained and agreed before CS closes the ticket, which is why
commenting is deliberately open to everyone in the tenant who can read it.

No internal-note vs reply distinction in v1: with no client portal there is
nothing to distinguish it from. Add it if a portal is ever built.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, Index, Text, text
from sqlalchemy.dialects.postgresql import UUID as PGUUID
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base
from app.core.models import (
    SoftDeleteMixin,
    TenantScopedMixin,
    TimestampMixin,
    UUIDPrimaryKeyMixin,
)


class Comment(Base, UUIDPrimaryKeyMixin, TenantScopedMixin, TimestampMixin, SoftDeleteMixin):
    __tablename__ = "comments"
    __table_args__ = (
        Index(
            "ix_comments_ticket_created",
            "ticket_id",
            "created_at",
            postgresql_where=text("deleted_at IS NULL"),
        ),
    )

    ticket_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("tickets.id", ondelete="CASCADE"), nullable=False
    )
    author_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("users.id", ondelete="RESTRICT"), nullable=False
    )
    body: Mapped[str] = mapped_column(Text, nullable=False)
    edited_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
