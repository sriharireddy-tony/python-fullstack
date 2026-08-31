"""Identity models: users, team membership, and refresh tokens."""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import (
    Boolean,
    DateTime,
    ForeignKey,
    Index,
    String,
    text,
)
from sqlalchemy.dialects.postgresql import CITEXT, JSONB
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
from app.core.permissions import Role


class User(Base, UUIDPrimaryKeyMixin, TenantScopedMixin, TimestampMixin, SoftDeleteMixin):
    """A person inside a tenant.

    Email is **globally unique**, not unique per tenant, so login stays a single
    field with no "which company are you?" step. The trade-off is that one
    person cannot hold accounts in two tenants; if that ever becomes real, move
    to per-tenant uniqueness plus a tenant slug in the URL.
    See docs/04-database.md.
    """

    __tablename__ = "users"
    __table_args__ = (
        Index(
            "uq_users_email_lower",
            text("lower(email)"),
            unique=True,
            postgresql_where=text("deleted_at IS NULL"),
        ),
        Index("ix_users_tenant_active", "tenant_id", "is_active"),
    )

    email: Mapped[str] = mapped_column(CITEXT, nullable=False)
    password_hash: Mapped[str] = mapped_column(String(255), nullable=False)
    full_name: Mapped[str] = mapped_column(String(200), nullable=False)
    role: Mapped[Role] = mapped_column(pg_enum(Role, "user_role"), nullable=False)

    # {"theme": "dark", "density": "compact"} -- the source of truth for
    # preferences; localStorage on the client is only a flash-prevention cache.
    preferences: Mapped[dict[str, object]] = mapped_column(
        JSONB, nullable=False, server_default=text("'{}'::jsonb")
    )

    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    last_login_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    def __repr__(self) -> str:
        return f"<User {self.email} ({self.role})>"


class UserTeam(Base):
    """Team membership.

    A join table even though one team per user is expected today, so multi-team
    membership later needs no migration.
    """

    __tablename__ = "user_teams"

    user_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), primary_key=True
    )
    team_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("teams.id", ondelete="CASCADE"), primary_key=True
    )
    is_primary: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)


class RefreshToken(Base, UUIDPrimaryKeyMixin):
    """A refresh-token record.

    Two details carry real weight:

    * **The hash is stored, never the token.** A database leak must not hand
      over live sessions.
    * **``family_id`` implements reuse detection.** Every refresh rotates the
      token and links the replacement to the same family. If an
      already-rotated token is presented again, that is theft -- the whole
      family is revoked, which logs out the attacker and the real user, who can
      then sign in again.

    This table is also what makes JWT revocable at all: a signed token is
    otherwise valid until it expires, even after a user is deactivated.
    """

    __tablename__ = "refresh_tokens"
    __table_args__ = (
        Index("ix_refresh_tokens_user_active", "user_id", "revoked_at"),
        Index("ix_refresh_tokens_family", "family_id"),
    )

    user_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    token_hash: Mapped[str] = mapped_column(String(64), nullable=False, unique=True)
    family_id: Mapped[uuid.UUID] = mapped_column(PGUUID(as_uuid=True), nullable=False)

    issued_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()")
    )
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    replaced_by_id: Mapped[uuid.UUID | None] = mapped_column(PGUUID(as_uuid=True))

    user_agent: Mapped[str | None] = mapped_column(String(400))
    ip_address: Mapped[str | None] = mapped_column(String(64))

    @property
    def is_active(self) -> bool:
        return self.revoked_at is None
