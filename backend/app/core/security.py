"""Password hashing, JWT issuing, and CSRF tokens.

Design decisions encoded here, from docs/08-security.md:

* **Argon2id** for passwords, with automatic rehash when parameters change.
* **JWT is the token format; the httpOnly cookie is the transport.** They are
  not alternatives. The application renders user-supplied ticket text, which is
  where XSS comes from -- a token in localStorage means one missed escape leaks
  every session.
* **The role goes in the token, not the permission list.** Embedding permissions
  makes every issued token stale the moment a role's permissions change.
* **Refresh tokens are stored hashed.** A database leak must not hand over live
  sessions.
"""

from __future__ import annotations

import contextlib
import hashlib
import hmac
import secrets
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, Literal

import jwt
from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerifyMismatchError

from app.core.config import settings

# --------------------------------------------------------------- passwords

_hasher = PasswordHasher()

# A fixed hash used to spend the same time on a missing account as on a real
# one. Without it, response timing reveals which email addresses exist.
_DUMMY_HASH = _hasher.hash("timing-attack-mitigation-placeholder")


def hash_password(password: str) -> str:
    return _hasher.hash(password)


def verify_password(password: str, password_hash: str) -> bool:
    try:
        return _hasher.verify(password_hash, password)
    except (VerifyMismatchError, InvalidHashError):
        return False


def needs_rehash(password_hash: str) -> bool:
    """True when the hash used weaker parameters than the current settings."""
    try:
        return _hasher.check_needs_rehash(password_hash)
    except InvalidHashError:
        return True


def dummy_verify() -> None:
    """Burn the same time as a real verification, for unknown accounts."""
    with contextlib.suppress(VerifyMismatchError, InvalidHashError):
        _hasher.verify(_DUMMY_HASH, "wrong-password")


# ------------------------------------------------------------------ tokens

TokenType = Literal["access", "refresh"]


@dataclass(frozen=True, slots=True)
class TokenClaims:
    subject: uuid.UUID
    token_type: TokenType
    jti: str
    expires_at: datetime
    tenant_id: uuid.UUID | None = None
    role: str | None = None
    is_platform_admin: bool = False


class TokenError(Exception):
    """Token is missing, malformed, expired, or has the wrong type."""


def _encode(payload: dict[str, Any]) -> str:
    return jwt.encode(payload, settings.JWT_SECRET, algorithm=settings.JWT_ALGORITHM)


def create_access_token(
    *,
    subject: uuid.UUID,
    tenant_id: uuid.UUID | None,
    role: str | None,
    is_platform_admin: bool = False,
) -> tuple[str, str, datetime]:
    """Return ``(token, jti, expires_at)``.

    The jti is returned so the caller can add it to the revocation denylist on
    logout -- a signed token is otherwise valid until it expires.
    """
    now = datetime.now(UTC)
    expires_at = now + timedelta(minutes=settings.ACCESS_TOKEN_TTL_MINUTES)
    jti = secrets.token_urlsafe(16)

    payload: dict[str, Any] = {
        "sub": str(subject),
        "typ": "access",
        "jti": jti,
        "iat": int(now.timestamp()),
        "exp": int(expires_at.timestamp()),
        "adm": is_platform_admin,
    }
    if tenant_id is not None:
        payload["tid"] = str(tenant_id)
    if role is not None:
        payload["rol"] = role

    return _encode(payload), jti, expires_at


def create_refresh_token(
    *, subject: uuid.UUID, tenant_id: uuid.UUID | None = None
) -> tuple[str, str, datetime]:
    """Issue a refresh token.

    The tenant is carried here as well as on the access token. Refresh happens
    with no request context, and the user row sits behind row-level security --
    without a tenant to scope by, it could not be loaded. The value is signed
    and is re-checked against the stored token row, so it is not client input.
    """
    now = datetime.now(UTC)
    expires_at = now + timedelta(days=settings.REFRESH_TOKEN_TTL_DAYS)
    jti = secrets.token_urlsafe(24)

    payload: dict[str, Any] = {
        "sub": str(subject),
        "typ": "refresh",
        "jti": jti,
        "iat": int(now.timestamp()),
        "exp": int(expires_at.timestamp()),
    }
    if tenant_id is not None:
        payload["tid"] = str(tenant_id)
    return _encode(payload), jti, expires_at


def decode_token(token: str, *, expected_type: TokenType) -> TokenClaims:
    try:
        payload = jwt.decode(
            token,
            settings.JWT_SECRET,
            algorithms=[settings.JWT_ALGORITHM],
            options={"require": ["exp", "sub", "jti", "typ"]},
        )
    except jwt.ExpiredSignatureError as exc:
        raise TokenError("Token has expired") from exc
    except jwt.InvalidTokenError as exc:
        raise TokenError("Token is invalid") from exc

    # An access token presented where a refresh token is expected (or the
    # reverse) must be rejected -- otherwise a long-lived refresh token would
    # be usable as an access token, defeating the short access TTL entirely.
    if payload.get("typ") != expected_type:
        raise TokenError(f"Expected a {expected_type} token")

    try:
        subject = uuid.UUID(payload["sub"])
    except (KeyError, ValueError) as exc:
        raise TokenError("Token subject is invalid") from exc

    tenant_raw = payload.get("tid")
    try:
        tenant_id = uuid.UUID(tenant_raw) if tenant_raw else None
    except ValueError as exc:
        raise TokenError("Token tenant is invalid") from exc

    return TokenClaims(
        subject=subject,
        token_type=expected_type,
        jti=str(payload["jti"]),
        expires_at=datetime.fromtimestamp(payload["exp"], tz=UTC),
        tenant_id=tenant_id,
        role=payload.get("rol"),
        is_platform_admin=bool(payload.get("adm", False)),
    )


def hash_token(token: str) -> str:
    """Hash a refresh token for storage.

    SHA-256 rather than Argon2 on purpose: the input is already 24 bytes of
    cryptographic randomness, so there is nothing to brute-force and no reason
    to pay a slow KDF on every refresh.
    """
    return hashlib.sha256(token.encode()).hexdigest()


# -------------------------------------------------------------------- CSRF


def generate_csrf_token() -> str:
    return secrets.token_urlsafe(32)


def csrf_tokens_match(cookie_value: str | None, header_value: str | None) -> bool:
    """Double-submit comparison, in constant time."""
    if not cookie_value or not header_value:
        return False
    return hmac.compare_digest(cookie_value, header_value)
