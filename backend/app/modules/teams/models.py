"""Team model.

Teams are admin-managed lookup data, not a hardcoded enum, so adding a team is
data entry rather than a deployment. Launch set: Platform, CoreHR, Payroll, UI,
Performance, Workforce.

Storing teams as rows also means the product-module dimension can be added
later as a purely additive change -- see docs/11-decisions-and-risks.md.
"""

from __future__ import annotations

import uuid

from sqlalchemy import Boolean, ForeignKey, Index, String, text
from sqlalchemy.dialects.postgresql import UUID as PGUUID
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base
from app.core.models import (
    SoftDeleteMixin,
    TenantScopedMixin,
    TimestampMixin,
    UUIDPrimaryKeyMixin,
)


class Team(Base, UUIDPrimaryKeyMixin, TenantScopedMixin, TimestampMixin, SoftDeleteMixin):
    __tablename__ = "teams"
    __table_args__ = (
        # Case-insensitive uniqueness within a tenant, so "Payroll" and
        # "payroll" cannot both exist and split the queue in two.
        # Partial on deleted_at so a soft-deleted team does not block reusing
        # its name.
        Index(
            "uq_teams_tenant_name_lower",
            "tenant_id",
            text("lower(name)"),
            unique=True,
            postgresql_where=text("deleted_at IS NULL"),
        ),
        Index(
            "uq_teams_tenant_code_lower",
            "tenant_id",
            text("lower(code)"),
            unique=True,
            postgresql_where=text("deleted_at IS NULL"),
        ),
    )

    name: Mapped[str] = mapped_column(String(120), nullable=False)
    code: Mapped[str] = mapped_column(String(50), nullable=False)
    description: Mapped[str | None] = mapped_column(String(500))
    manager_id: Mapped[uuid.UUID | None] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL")
    )
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)

    def __repr__(self) -> str:
        return f"<Team {self.code}>"
