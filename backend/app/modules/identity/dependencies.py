"""Authentication and authorization dependencies.

These implement layers 1 and 2 of the three-layer model in
docs/02-roles-and-permissions.md:

  Layer 1  tenant isolation  -- tenant read from the token, set as a Postgres
                                session variable, enforced by RLS
  Layer 2  permission (RBAC) -- ``require(Permission.X)`` on the route
  Layer 3  resource policy   -- in each module's service/policies, not here

Living in the identity module rather than in ``core`` keeps the dependency
direction right: core knows nothing about modules.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import Annotated

from fastapi import Depends, Request
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.context import set_tenant_id, set_user_id
from app.core.database import SessionFactory, apply_tenant_scope
from app.core.errors import AuthenticationError, PermissionDeniedError
from app.core.permissions import Permission, Role, permissions_for
from app.core.security import TokenError, csrf_tokens_match, decode_token
from app.core.throttle import denylist

ACCESS_COOKIE = "access_token"
REFRESH_COOKIE = "refresh_token"
CSRF_COOKIE = "csrf_token"
CSRF_HEADER = "X-CSRF-Token"

_UNSAFE_METHODS = frozenset({"POST", "PUT", "PATCH", "DELETE"})


@dataclass(frozen=True, slots=True)
class RequestContext:
    """Everything the service layer needs about who is calling.

    Built entirely from the verified token. Nothing here is ever read from a
    path parameter, query string, or request body -- a request cannot ask to
    act as another user or in another tenant.
    """

    user_id: uuid.UUID
    tenant_id: uuid.UUID | None
    role: Role | None
    permissions: frozenset[Permission]
    is_platform_admin: bool
    access_jti: str

    def has(self, permission: Permission) -> bool:
        return permission in self.permissions

    def require(self, permission: Permission) -> None:
        if not self.has(permission):
            raise PermissionDeniedError("You do not have permission to perform this action.")

    @property
    def tenant(self) -> uuid.UUID:
        if self.tenant_id is None:
            raise PermissionDeniedError("This action requires a tenant context.")
        return self.tenant_id


def _verify_csrf(request: Request) -> None:
    """Double-submit CSRF check on state-changing requests.

    Required because authentication rides on cookies: the browser attaches them
    to cross-site requests automatically, and only a value the attacker cannot
    read proves the request came from our own page.
    """
    if request.method not in _UNSAFE_METHODS:
        return
    cookie = request.cookies.get(CSRF_COOKIE)
    header = request.headers.get(CSRF_HEADER)
    if not csrf_tokens_match(cookie, header):
        raise PermissionDeniedError("CSRF validation failed. Reload the page and try again.")


async def get_request_context(request: Request) -> RequestContext:
    token = request.cookies.get(ACCESS_COOKIE)
    if not token:
        raise AuthenticationError("Not authenticated.")

    try:
        claims = decode_token(token, expected_type="access")
    except TokenError as exc:
        raise AuthenticationError("Session expired. Sign in again.") from exc

    # A logged-out token stays cryptographically valid until it expires, so the
    # denylist is checked on every request. It keeps an in-process copy, so
    # logout is still honoured on this instance when Redis is unavailable.
    if await denylist.contains(claims.jti):
        raise AuthenticationError("Session ended. Sign in again.")

    _verify_csrf(request)

    role = Role(claims.role) if claims.role else None
    permissions = (
        frozenset({Permission.TENANT_MANAGE, Permission.AUDIT_READ})
        if claims.is_platform_admin
        else frozenset(permissions_for(role))
        if role
        else frozenset()
    )

    # Bind context so logging correlates and the database session scopes.
    set_user_id(claims.subject)
    set_tenant_id(claims.tenant_id)

    return RequestContext(
        user_id=claims.subject,
        tenant_id=claims.tenant_id,
        role=role,
        permissions=permissions,
        is_platform_admin=claims.is_platform_admin,
        access_jti=claims.jti,
    )


CurrentUser = Annotated[RequestContext, Depends(get_request_context)]


async def get_scoped_db(ctx: CurrentUser):  # type: ignore[no-untyped-def]
    """Session scoped to the authenticated tenant.

    Ordering matters: this depends on the request context, so the tenant is
    always known before the transaction opens and ``SET LOCAL app.tenant_id``
    runs. Routes that use this can never issue an unscoped query.
    """
    async with SessionFactory() as session:
        await apply_tenant_scope(session, ctx.tenant_id)
        try:
            yield session
        except Exception:
            await session.rollback()
            raise
        else:
            await session.commit()


ScopedDb = Annotated[AsyncSession, Depends(get_scoped_db)]


def require(*permissions: Permission):  # type: ignore[no-untyped-def]
    """Route dependency enforcing one or more permissions (all required).

    Usage::

        Assign = Annotated[RequestContext, Depends(require(Permission.TICKET_ASSIGN))]

        @router.post("/tickets/{id}/assign")
        async def assign(ctx: Assign) -> ...:
            ...
    """

    async def _dependency(ctx: CurrentUser) -> RequestContext:
        for permission in permissions:
            ctx.require(permission)
        return ctx

    return _dependency


async def require_platform_admin(ctx: CurrentUser) -> RequestContext:
    if not ctx.is_platform_admin:
        raise PermissionDeniedError("Platform administrator access is required.")
    return ctx
