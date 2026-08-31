"""Database engine, session management, and tenant scoping.

The session dependency does two things beyond handing out a connection:

1. It opens a transaction that spans the whole request, so ``SET LOCAL`` stays
   in scope until the request finishes.

2. It sets ``app.tenant_id`` from the request context, which is what the
   row-level-security policies read. ``SET LOCAL`` matters: it resets when the
   transaction ends, so a pooled connection cannot leak one request's tenant
   into the next.

The tenant value always originates from the authenticated token. It is never
read from a path parameter, query string, or request body -- a request cannot
ask to act on another tenant.

See docs/04-database.md.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

from sqlalchemy import MetaData, text
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.orm import DeclarativeBase

from app.core.config import settings
from app.core.context import get_tenant_id

TENANT_SETTING = "app.tenant_id"

# Explicit constraint naming so Alembic autogenerate produces stable, readable
# migration names instead of database-assigned ones.
NAMING_CONVENTION = {
    "ix": "ix_%(table_name)s_%(column_0_N_name)s",
    "uq": "uq_%(table_name)s_%(column_0_N_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}


class Base(DeclarativeBase):
    """Declarative base for every ORM model."""

    metadata = MetaData(naming_convention=NAMING_CONVENTION)


def create_engine(url: str | None = None) -> AsyncEngine:
    return create_async_engine(
        url or settings.DATABASE_URL,
        echo=settings.DB_ECHO,
        pool_size=settings.DB_POOL_SIZE,
        max_overflow=settings.DB_MAX_OVERFLOW,
        pool_pre_ping=True,  # survives a database restart without stale connections
        future=True,
    )


engine: AsyncEngine = create_engine()

SessionFactory: async_sessionmaker[AsyncSession] = async_sessionmaker(
    bind=engine,
    class_=AsyncSession,
    expire_on_commit=False,  # objects stay usable after commit, for serialisation
    autoflush=False,
)


async def apply_tenant_scope(session: AsyncSession, tenant_id: uuid.UUID | None) -> None:
    """Set the RLS tenant scope for this transaction.

    When no tenant is present the setting is cleared rather than left over from
    a previous transaction. RLS policies use ``current_setting(..., true)``,
    which yields NULL when unset -- so an unset scope matches no rows. Failing
    closed is the correct default.
    """
    if tenant_id is None:
        await session.execute(text(f"SELECT set_config('{TENANT_SETTING}', '', true)"))
        return
    await session.execute(
        text(f"SELECT set_config('{TENANT_SETTING}', :tenant_id, true)"),
        {"tenant_id": str(tenant_id)},
    )


async def get_db() -> AsyncIterator[AsyncSession]:
    """FastAPI dependency yielding a request-scoped session.

    Commits on success, rolls back on any exception. Services never commit --
    the unit of work is the request, so a failure part-way through leaves
    nothing half-written.
    """
    async with SessionFactory() as session:
        await apply_tenant_scope(session, get_tenant_id())
        try:
            yield session
        except Exception:
            await session.rollback()
            raise
        else:
            await session.commit()


@asynccontextmanager
async def session_scope(tenant_id: uuid.UUID | None = None) -> AsyncIterator[AsyncSession]:
    """Session for code outside the request cycle: CLI commands, jobs, seeds."""
    async with SessionFactory() as session:
        await apply_tenant_scope(session, tenant_id)
        try:
            yield session
        except Exception:
            await session.rollback()
            raise
        else:
            await session.commit()


async def check_database() -> dict[str, Any]:
    """Readiness probe. Never raises -- the caller decides what to do."""
    try:
        async with engine.connect() as conn:
            await conn.execute(text("SELECT 1"))
        return {"status": "ok"}
    except Exception as exc:
        return {"status": "error", "error": type(exc).__name__}


async def dispose_engine() -> None:
    await engine.dispose()
