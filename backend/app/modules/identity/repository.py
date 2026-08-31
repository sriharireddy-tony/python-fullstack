"""Data access for the identity module.

Queries only. No business rules, no authorization decisions -- those belong in
the service and policy layers.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Any, cast

from sqlalchemy import CursorResult, select, text, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.modules.identity.models import RefreshToken, User, UserTeam
from app.modules.teams.models import Team
from app.modules.tenancy.models import PlatformAdmin


class UserRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def get_by_id(self, user_id: uuid.UUID) -> User | None:
        result = await self._session.execute(
            select(User).where(User.id == user_id, User.deleted_at.is_(None))
        )
        return result.scalar_one_or_none()

    async def tenant_for_email(self, email: str) -> uuid.UUID | None:
        """Which tenant owns this email address?

        Calls a SECURITY DEFINER function (migration 0003) because ``users`` is
        behind FORCE row-level security and login happens before any tenant is
        known. The function returns a single uuid and nothing else, so it cannot
        be used to read another tenant's data.

        The caller sets the tenant scope from this, then loads the user through
        ordinary RLS.
        """
        result = await self._session.execute(
            text("SELECT auth_tenant_for_email(:email)"), {"email": email}
        )
        value = result.scalar_one_or_none()
        return uuid.UUID(str(value)) if value else None

    async def get_by_email(self, email: str) -> User | None:
        """Load a user by email within the current tenant scope."""
        result = await self._session.execute(
            select(User).where(User.email == email, User.deleted_at.is_(None))
        )
        return result.scalar_one_or_none()

    async def teams_for(self, user_id: uuid.UUID) -> list[Team]:
        result = await self._session.execute(
            select(Team)
            .join(UserTeam, UserTeam.team_id == Team.id)
            .where(UserTeam.user_id == user_id, Team.deleted_at.is_(None))
            .order_by(UserTeam.is_primary.desc(), Team.name)
        )
        return list(result.scalars().all())

    async def touch_last_login(self, user_id: uuid.UUID) -> None:
        await self._session.execute(
            update(User).where(User.id == user_id).values(last_login_at=datetime.now(UTC))
        )

    async def set_password_hash(self, user_id: uuid.UUID, password_hash: str) -> None:
        await self._session.execute(
            update(User).where(User.id == user_id).values(password_hash=password_hash)
        )


class PlatformAdminRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def get_by_id(self, admin_id: uuid.UUID) -> PlatformAdmin | None:
        result = await self._session.execute(
            select(PlatformAdmin).where(
                PlatformAdmin.id == admin_id, PlatformAdmin.deleted_at.is_(None)
            )
        )
        return result.scalar_one_or_none()

    async def get_by_email(self, email: str) -> PlatformAdmin | None:
        result = await self._session.execute(
            select(PlatformAdmin).where(
                PlatformAdmin.email == email, PlatformAdmin.deleted_at.is_(None)
            )
        )
        return result.scalar_one_or_none()

    async def touch_last_login(self, admin_id: uuid.UUID) -> None:
        from sqlalchemy import update as sa_update

        await self._session.execute(
            sa_update(PlatformAdmin)
            .where(PlatformAdmin.id == admin_id)
            .values(last_login_at=datetime.now(UTC))
        )


class RefreshTokenRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def add(self, token: RefreshToken) -> RefreshToken:
        self._session.add(token)
        await self._session.flush()
        return token

    async def get_by_hash(self, token_hash: str) -> RefreshToken | None:
        result = await self._session.execute(
            select(RefreshToken).where(RefreshToken.token_hash == token_hash)
        )
        return result.scalar_one_or_none()

    async def revoke(self, token_id: uuid.UUID, replaced_by_id: uuid.UUID | None = None) -> None:
        await self._session.execute(
            update(RefreshToken)
            .where(RefreshToken.id == token_id, RefreshToken.revoked_at.is_(None))
            .values(revoked_at=datetime.now(UTC), replaced_by_id=replaced_by_id)
        )

    async def revoke_family(self, family_id: uuid.UUID) -> int:
        """Revoke every token in a lineage. Used on reuse detection."""
        result = cast(
            CursorResult[Any],
            await self._session.execute(
                update(RefreshToken)
                .where(RefreshToken.family_id == family_id, RefreshToken.revoked_at.is_(None))
                .values(revoked_at=datetime.now(UTC))
            ),
        )
        return result.rowcount or 0

    async def revoke_all_for_user(self, user_id: uuid.UUID) -> int:
        """Used on password change and on deactivation."""
        result = cast(
            CursorResult[Any],
            await self._session.execute(
                update(RefreshToken)
                .where(RefreshToken.user_id == user_id, RefreshToken.revoked_at.is_(None))
                .values(revoked_at=datetime.now(UTC))
            ),
        )
        return result.rowcount or 0
