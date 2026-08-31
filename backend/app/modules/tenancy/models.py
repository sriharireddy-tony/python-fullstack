"""Tenancy models.

``Tenant`` and ``TenantCounter`` are the roots of the multi-tenant model.
``PlatformAdmin`` deliberately sits outside it.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import Boolean, DateTime, ForeignKey, Integer, String, text
from sqlalchemy.dialects.postgresql import CITEXT
from sqlalchemy.dialects.postgresql import UUID as PGUUID
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base
from app.core.models import SoftDeleteMixin, TimestampMixin, UUIDPrimaryKeyMixin


class Tenant(Base, UUIDPrimaryKeyMixin, TimestampMixin, SoftDeleteMixin):
    """An organisation using OS Tracker."""

    __tablename__ = "tenants"

    name: Mapped[str] = mapped_column(String(200), nullable=False)
    code: Mapped[str] = mapped_column(String(50), nullable=False, unique=True)
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)

    def __repr__(self) -> str:
        return f"<Tenant {self.code}>"


class PlatformAdmin(Base, UUIDPrimaryKeyMixin, TimestampMixin, SoftDeleteMixin):
    """Platform super admin -- sits above every tenant.

    A separate table from ``users``, with no ``tenant_id``, on purpose. Keeping
    super admins out of the tenant-scoped table means there is no code path
    where a bug could turn a normal user into a cross-tenant one: the isolation
    is a property of the schema rather than of an ``if`` statement.

    Their permissions cover tenant management only -- deliberately not ticket
    content. An account able to read every tenant's HR support data would be the
    highest-value target in the system.
    """

    __tablename__ = "platform_admins"

    email: Mapped[str] = mapped_column(CITEXT, nullable=False, unique=True)
    password_hash: Mapped[str] = mapped_column(String(255), nullable=False)
    full_name: Mapped[str] = mapped_column(String(200), nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    last_login_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    def __repr__(self) -> str:
        return f"<PlatformAdmin {self.email}>"


class TenantCounter(Base):
    """Per-tenant ticket number sequence.

    Not a Postgres sequence, because that would need one sequence per tenant.
    Not ``MAX(ticket_number) + 1`` either: two agents submitting simultaneously
    would produce duplicate numbers -- a race that only appears under real use.
    The row is locked with ``SELECT ... FOR UPDATE`` inside the creating
    transaction. See docs/04-database.md.
    """

    __tablename__ = "tenant_counters"

    tenant_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey("tenants.id", ondelete="CASCADE"),
        primary_key=True,
    )
    last_ticket_number: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default=text("0")
    )
