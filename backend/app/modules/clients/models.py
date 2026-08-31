"""Client model -- the customer companies whose issues are reported.

Clients are data, never users: they do not authenticate and have no access to
OS Tracker. Recording them is what makes "which customers are affected by this
bug?" and "show everything open for Acme before their renewal" answerable --
questions that cannot be backfilled if the data was never captured.
"""

from __future__ import annotations

from enum import StrEnum

from sqlalchemy import Boolean, Index, String, Text, text
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base
from app.core.models import (
    SoftDeleteMixin,
    TenantScopedMixin,
    TimestampMixin,
    UUIDPrimaryKeyMixin,
    pg_enum,
)


class ClientTier(StrEnum):
    STANDARD = "standard"
    PREMIUM = "premium"
    ENTERPRISE = "enterprise"


class Client(Base, UUIDPrimaryKeyMixin, TenantScopedMixin, TimestampMixin, SoftDeleteMixin):
    __tablename__ = "clients"
    __table_args__ = (
        # Partial on deleted_at so a soft-deleted client does not permanently
        # reserve its name.
        Index(
            "uq_clients_tenant_name_lower",
            "tenant_id",
            text("lower(name)"),
            unique=True,
            postgresql_where=text("deleted_at IS NULL"),
        ),
        Index(
            "uq_clients_tenant_code_lower",
            "tenant_id",
            text("lower(code)"),
            unique=True,
            postgresql_where=text("deleted_at IS NULL"),
        ),
        Index("ix_clients_tenant_active", "tenant_id", "is_active"),
    )

    name: Mapped[str] = mapped_column(String(200), nullable=False)
    code: Mapped[str] = mapped_column(String(50), nullable=False)

    # Stored now although SLA is deferred: one column, and it is what SLA will
    # key off when it arrives. Retrofitting it onto historical tickets would
    # leave them permanently blank.
    tier: Mapped[ClientTier] = mapped_column(
        pg_enum(ClientTier, "client_tier"),
        nullable=False,
        default=ClientTier.STANDARD,
        server_default=ClientTier.STANDARD.value,
    )

    notes: Mapped[str | None] = mapped_column(Text)
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)

    def __repr__(self) -> str:
        return f"<Client {self.code}>"
