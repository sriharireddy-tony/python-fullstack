"""Request-scoped context.

Context variables carry the request id, tenant, and user through the whole call
stack without threading them through every function signature. Logging reads
them so that *every* line emitted during a request is correlated automatically,
and the database session reads the tenant to set the row-level-security scope.

Context variables are async-safe: each request gets its own values even when
many are in flight on the same event loop.
"""

from __future__ import annotations

import uuid
from contextvars import ContextVar, Token
from dataclasses import dataclass

_request_id: ContextVar[str | None] = ContextVar("request_id", default=None)
_tenant_id: ContextVar[uuid.UUID | None] = ContextVar("tenant_id", default=None)
_user_id: ContextVar[uuid.UUID | None] = ContextVar("user_id", default=None)


def get_request_id() -> str | None:
    return _request_id.get()


def set_request_id(value: str) -> Token[str | None]:
    return _request_id.set(value)


def get_tenant_id() -> uuid.UUID | None:
    return _tenant_id.get()


def set_tenant_id(value: uuid.UUID | None) -> Token[uuid.UUID | None]:
    return _tenant_id.set(value)


def get_user_id() -> uuid.UUID | None:
    return _user_id.get()


def set_user_id(value: uuid.UUID | None) -> Token[uuid.UUID | None]:
    return _user_id.set(value)


def require_tenant_id() -> uuid.UUID:
    """Tenant id for the current request, or fail.

    Tenant scope always originates from the authenticated token -- never from a
    path parameter, query string, or request body. If it is missing here, the
    request reached tenant-scoped code without authentication, which is a bug
    rather than a client error.
    """
    tenant = _tenant_id.get()
    if tenant is None:
        raise RuntimeError(
            "No tenant in request context. Tenant-scoped code was reached "
            "without an authenticated tenant."
        )
    return tenant


@dataclass(frozen=True, slots=True)
class LogContext:
    """Snapshot of the current context, for log records."""

    request_id: str | None
    tenant_id: uuid.UUID | None
    user_id: uuid.UUID | None

    @classmethod
    def current(cls) -> LogContext:
        return cls(
            request_id=_request_id.get(),
            tenant_id=_tenant_id.get(),
            user_id=_user_id.get(),
        )
