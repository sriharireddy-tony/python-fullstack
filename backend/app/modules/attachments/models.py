"""Attachment metadata.

The bytes live in the storage backend; only metadata lives here. ``storage_key``
is opaque -- no paths, no URLs, no filesystem structure encoded in it. That is
what makes the local-disk to Azure Blob switch a configuration change rather
than a data migration.
"""

from __future__ import annotations

import uuid

from sqlalchemy import BigInteger, CheckConstraint, ForeignKey, Index, String, text
from sqlalchemy.dialects.postgresql import UUID as PGUUID
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base
from app.core.models import (
    SoftDeleteMixin,
    TenantScopedMixin,
    TimestampMixin,
    UUIDPrimaryKeyMixin,
)


class Attachment(Base, UUIDPrimaryKeyMixin, TenantScopedMixin, TimestampMixin, SoftDeleteMixin):
    __tablename__ = "attachments"
    __table_args__ = (
        # An attachment belongs to exactly one parent. Enforced in the database
        # so no code path can create an orphan or a double-parented row.
        CheckConstraint(
            "(ticket_id IS NOT NULL AND comment_id IS NULL) "
            "OR (ticket_id IS NULL AND comment_id IS NOT NULL)",
            name="exactly_one_parent",
        ),
        Index(
            "ix_attachments_ticket",
            "ticket_id",
            postgresql_where=text("deleted_at IS NULL"),
        ),
        Index(
            "ix_attachments_comment",
            "comment_id",
            postgresql_where=text("deleted_at IS NULL"),
        ),
    )

    ticket_id: Mapped[uuid.UUID | None] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("tickets.id", ondelete="CASCADE")
    )
    comment_id: Mapped[uuid.UUID | None] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("comments.id", ondelete="CASCADE")
    )
    uploaded_by_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("users.id", ondelete="RESTRICT"), nullable=False
    )

    #: Sanitised, and metadata only -- never used to build a filesystem path.
    original_filename: Mapped[str] = mapped_column(String(255), nullable=False)
    storage_key: Mapped[str] = mapped_column(String(500), nullable=False, unique=True)
    content_type: Mapped[str] = mapped_column(String(120), nullable=False)
    size_bytes: Mapped[int] = mapped_column(BigInteger, nullable=False)
    checksum: Mapped[str] = mapped_column(String(64), nullable=False)
