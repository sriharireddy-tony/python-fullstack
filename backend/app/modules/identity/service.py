"""Authentication and session business rules.

No HTTP in this layer. It returns issued tokens; the router decides how to put
them on the wire. That keeps the same rules available to a CLI command, a
seed script, or future automation.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.cache import Cache
from app.core.database import apply_tenant_scope
from app.core.errors import (
    AuthenticationError,
    PermissionDeniedError,
    RateLimitError,
    ValidationFailedError,
)
from app.core.logging import get_logger
from app.core.permissions import PLATFORM_ADMIN_PERMISSIONS, Role, permissions_for
from app.core.security import (
    TokenError,
    create_access_token,
    create_refresh_token,
    decode_token,
    dummy_verify,
    hash_password,
    hash_token,
    needs_rehash,
    verify_password,
)
from app.core.throttle import (
    LOGIN_PER_ACCOUNT,
    LOGIN_PER_IP,
    denylist,
    rate_limiter,
)
from app.modules.identity.models import RefreshToken, User
from app.modules.identity.repository import (
    PlatformAdminRepository,
    RefreshTokenRepository,
    UserRepository,
)
from app.modules.identity.schemas import SessionResponse, TeamSummary, UserPreferences
from app.modules.tenancy.models import PlatformAdmin

logger = get_logger(__name__)


@dataclass(frozen=True, slots=True)
class IssuedTokens:
    access_token: str
    access_jti: str
    access_expires_at: datetime
    refresh_token: str
    refresh_expires_at: datetime
    csrf_token: str


@dataclass(frozen=True, slots=True)
class LoginResult:
    tokens: IssuedTokens
    session: SessionResponse


class AuthService:
    def __init__(self, session: AsyncSession, cache: Cache | None = None) -> None:
        self._session = session
        self._users = UserRepository(session)
        self._admins = PlatformAdminRepository(session)
        self._tokens = RefreshTokenRepository(session)
        self._cache = cache or Cache()

    # ----------------------------------------------------------- login

    async def login(
        self,
        email: str,
        password: str,
        *,
        user_agent: str | None = None,
        ip_address: str | None = None,
    ) -> LoginResult:
        email = email.strip().lower()

        # Rate limited before any password work: brute force should be cheap to
        # refuse and expensive to attempt, not the other way round. Limited per
        # account and per IP, because either alone leaves an obvious gap.
        for scope, identifier, rule in (
            ("login:account", email, LOGIN_PER_ACCOUNT),
            ("login:ip", ip_address or "unknown", LOGIN_PER_IP),
        ):
            allowed, count = await rate_limiter.hit(scope, identifier, rule)
            if not allowed:
                logger.warning("login rate limited", extra={"scope": scope, "attempts": count})
                raise RateLimitError("Too many sign-in attempts. Wait a few minutes and try again.")

        # Resolve the tenant before touching `users`: that table is behind
        # FORCE row-level security, so an unscoped query returns nothing. The
        # lookup returns only a tenant id (migration 0003).
        tenant_id = await self._users.tenant_for_email(email)

        user: User | None = None
        if tenant_id is not None:
            await apply_tenant_scope(self._session, tenant_id)
            user = await self._users.get_by_email(email)

        # platform_admins is deliberately not tenant-scoped, so it is queried
        # directly and only when no tenant user matched.
        admin = None if user else await self._admins.get_by_email(email)
        principal: User | PlatformAdmin | None = user or admin

        if principal is None:
            # Spend the same time as a real verification so response timing
            # does not reveal which email addresses exist.
            dummy_verify()
            logger.warning("login failed: unknown account")
            raise AuthenticationError("Email or password is incorrect.")

        if not verify_password(password, principal.password_hash):
            logger.warning("login failed: bad password", extra={"user_id": str(principal.id)})
            raise AuthenticationError("Email or password is incorrect.")

        if not principal.is_active:
            # Same message as a bad password on purpose: distinguishing them
            # tells an attacker which accounts are real but disabled.
            logger.warning("login failed: inactive account", extra={"user_id": str(principal.id)})
            raise AuthenticationError("Email or password is incorrect.")

        # Transparently upgrade the hash when Argon2 parameters have moved on.
        if needs_rehash(principal.password_hash) and isinstance(principal, User):
            await self._users.set_password_hash(principal.id, hash_password(password))

        if isinstance(principal, User):
            await self._users.touch_last_login(principal.id)
        else:
            await self._admins.touch_last_login(principal.id)

        tokens = await self._issue_tokens(
            principal, family_id=None, user_agent=user_agent, ip_address=ip_address
        )
        session = await self.build_session(principal)

        await rate_limiter.reset("login:account", email)

        logger.info(
            "login succeeded",
            extra={"user_id": str(principal.id), "is_platform_admin": admin is not None},
        )
        return LoginResult(tokens=tokens, session=session)

    # --------------------------------------------------------- refresh

    async def refresh(
        self,
        refresh_token: str,
        *,
        user_agent: str | None = None,
        ip_address: str | None = None,
    ) -> LoginResult:
        try:
            claims = decode_token(refresh_token, expected_type="refresh")
        except TokenError as exc:
            raise AuthenticationError("Session expired. Sign in again.") from exc

        stored = await self._tokens.get_by_hash(hash_token(refresh_token))
        if stored is None:
            raise AuthenticationError("Session expired. Sign in again.")

        if stored.revoked_at is not None:
            # A token that was already rotated is being presented again. Either
            # it was stolen, or the legitimate client is replaying. Either way
            # the safe response is to destroy the whole lineage: the attacker
            # loses access, and the real user simply signs in again.
            revoked = await self._tokens.revoke_family(stored.family_id)

            # Committed here, and this is the one place a service commits.
            # Raising below unwinds to the session dependency, which rolls the
            # transaction back -- silently undoing the very revocation this
            # branch exists to perform. The revocation must outlive the error
            # that reports it.
            await self._session.commit()

            logger.warning(
                "refresh token reuse detected; family revoked",
                extra={"user_id": str(stored.user_id), "revoked_count": revoked},
            )
            raise AuthenticationError("Session expired. Sign in again.")

        if stored.expires_at <= datetime.now(UTC):
            raise AuthenticationError("Session expired. Sign in again.")

        # Scope from the signed token so the user row is readable. The value
        # was issued by us and the token row was just verified above, so this is
        # not client-controlled input.
        if claims.tenant_id is not None:
            await apply_tenant_scope(self._session, claims.tenant_id)

        principal = await self._load_principal(claims.subject)
        if principal is None or not principal.is_active:
            # Same reasoning as above: commit before the raise rolls it back.
            await self._tokens.revoke_family(stored.family_id)
            await self._session.commit()
            raise AuthenticationError("Session expired. Sign in again.")

        tokens = await self._issue_tokens(
            principal,
            family_id=stored.family_id,
            user_agent=user_agent,
            ip_address=ip_address,
            rotated_from=stored,
        )
        return LoginResult(tokens=tokens, session=await self.build_session(principal))

    # ---------------------------------------------------------- logout

    async def logout(self, refresh_token: str | None, access_jti: str | None) -> None:
        if refresh_token:
            stored = await self._tokens.get_by_hash(hash_token(refresh_token))
            if stored is not None:
                await self._tokens.revoke_family(stored.family_id)

        # The access token stays cryptographically valid until it expires, so
        # its id goes on a short-lived denylist. Without this, "log out" would
        # leave a usable token for up to the access TTL.
        if access_jti:
            await self._deny_access_token(access_jti)

    async def _deny_access_token(self, jti: str, ttl_seconds: int | None = None) -> None:
        from app.core.config import settings

        ttl = ttl_seconds or settings.ACCESS_TOKEN_TTL_MINUTES * 60
        await denylist.add(jti, ttl)

    async def is_access_token_denied(self, jti: str) -> bool:
        return await denylist.contains(jti)

    # ------------------------------------------------- change password

    async def change_password(
        self, user_id: uuid.UUID, current_password: str, new_password: str
    ) -> None:
        user = await self._users.get_by_id(user_id)
        if user is None:
            raise AuthenticationError("Not authenticated.")

        if not verify_password(current_password, user.password_hash):
            logger.warning("password change failed: wrong current password")
            raise PermissionDeniedError("Current password is incorrect.")

        if current_password == new_password:
            raise ValidationFailedError("The new password must differ from the current one.")

        await self._users.set_password_hash(user_id, hash_password(new_password))

        # Every other session is invalidated. If the password was changed
        # because it may have been compromised, leaving other sessions alive
        # would defeat the point.
        revoked = await self._tokens.revoke_all_for_user(user_id)
        logger.info(
            "password changed", extra={"user_id": str(user_id), "sessions_revoked": revoked}
        )

    # ---------------------------------------------------------- helpers

    async def _load_principal(self, subject: uuid.UUID) -> User | PlatformAdmin | None:
        user = await self._users.get_by_id(subject)
        if user is not None:
            return user
        return await self._admins.get_by_id(subject)

    async def _issue_tokens(
        self,
        principal: User | PlatformAdmin,
        *,
        family_id: uuid.UUID | None,
        user_agent: str | None,
        ip_address: str | None,
        rotated_from: RefreshToken | None = None,
    ) -> IssuedTokens:
        is_admin = isinstance(principal, PlatformAdmin)
        # Narrowed via isinstance rather than the boolean, so the type checker
        # can prove a platform admin never carries a tenant or a tenant role.
        tenant_id = None if isinstance(principal, PlatformAdmin) else principal.tenant_id
        role = None if isinstance(principal, PlatformAdmin) else principal.role.value

        access_token, access_jti, access_expires = create_access_token(
            subject=principal.id,
            tenant_id=tenant_id,
            role=role,
            is_platform_admin=is_admin,
        )
        refresh_token, _refresh_jti, refresh_expires = create_refresh_token(
            subject=principal.id, tenant_id=tenant_id
        )

        record = RefreshToken(
            user_id=principal.id,
            token_hash=hash_token(refresh_token),
            family_id=family_id or uuid.uuid4(),
            expires_at=refresh_expires,
            user_agent=(user_agent or "")[:400] or None,
            ip_address=(ip_address or "")[:64] or None,
        )
        await self._tokens.add(record)

        if rotated_from is not None:
            await self._tokens.revoke(rotated_from.id, replaced_by_id=record.id)

        from app.core.security import generate_csrf_token

        return IssuedTokens(
            access_token=access_token,
            access_jti=access_jti,
            access_expires_at=access_expires,
            refresh_token=refresh_token,
            refresh_expires_at=refresh_expires,
            csrf_token=generate_csrf_token(),
        )

    async def build_session(self, principal: User | PlatformAdmin) -> SessionResponse:
        if isinstance(principal, PlatformAdmin):
            return SessionResponse(
                id=principal.id,
                email=principal.email,
                full_name=principal.full_name,
                role=None,
                is_platform_admin=True,
                permissions=sorted(p.value for p in PLATFORM_ADMIN_PERMISSIONS),
                teams=[],
                preferences=UserPreferences(),
                tenant_id=None,
                last_login_at=principal.last_login_at,
            )

        teams = await self._users.teams_for(principal.id)
        prefs = principal.preferences or {}
        return SessionResponse(
            id=principal.id,
            email=principal.email,
            full_name=principal.full_name,
            role=principal.role,
            is_platform_admin=False,
            permissions=sorted(p.value for p in permissions_for(Role(principal.role))),
            teams=[TeamSummary(id=t.id, name=t.name) for t in teams],
            preferences=UserPreferences(
                theme=str(prefs.get("theme", "system")),
                density=str(prefs.get("density", "comfortable")),
            ),
            tenant_id=principal.tenant_id,
            last_login_at=principal.last_login_at,
        )
