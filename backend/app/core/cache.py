"""Redis cache, key building, and degradation behaviour.

Two rules from docs/09-logging-and-caching.md are enforced here rather than left
to discipline at call sites:

1. **Every tenant-scoped key starts with the tenant.** Keys are built by
   ``CacheKey``, never by string concatenation. A key without a tenant prefix is
   the same bug class as a missing ``WHERE tenant_id`` -- except row-level
   security cannot save you, because the database was never consulted.

2. **Failure behaviour is chosen per use case.** Caching fails open (miss, hit
   Postgres, log a warning). Rate limiting must *not* fail open, or a Redis
   outage becomes an open brute-force window on the login endpoint -- callers
   there use ``ping`` and fall back to an in-process limiter.
"""

from __future__ import annotations

import json
import uuid
from collections.abc import Awaitable, Callable
from typing import Any, TypeVar

import redis.asyncio as aioredis
from redis.asyncio import Redis
from redis.exceptions import RedisError

from app.core.config import settings
from app.core.logging import get_logger

logger = get_logger(__name__)

T = TypeVar("T")

_client: Redis | None = None


def get_redis() -> Redis:
    global _client
    if _client is None:
        _client = aioredis.from_url(
            settings.REDIS_URL,
            encoding="utf-8",
            decode_responses=True,
            socket_connect_timeout=2,
            socket_timeout=2,
            health_check_interval=30,
        )
    return _client


async def close_redis() -> None:
    global _client
    if _client is not None:
        await _client.aclose()
        _client = None


# --------------------------------------------------------------------- keys


class CacheKey:
    """Namespaced key builder.

    Tenant-scoped helpers take the tenant as their first argument, so it is not
    possible to build a tenant key without one.
    """

    @staticmethod
    def tenant(tenant_id: uuid.UUID, *parts: str) -> str:
        return ":".join(("tenant", str(tenant_id), *parts))

    @staticmethod
    def global_(*parts: str) -> str:
        return ":".join(("global", *parts))

    # --- concrete keys, so call sites never assemble strings themselves ---

    @classmethod
    def teams_list(cls, tenant_id: uuid.UUID) -> str:
        return cls.tenant(tenant_id, "teams", "list")

    @classmethod
    def clients_list(cls, tenant_id: uuid.UUID) -> str:
        return cls.tenant(tenant_id, "clients", "list")

    @classmethod
    def dashboard_counts(cls, tenant_id: uuid.UUID, user_id: uuid.UUID) -> str:
        return cls.tenant(tenant_id, "dashboard", str(user_id), "counts")

    @classmethod
    def tenant_prefix(cls, tenant_id: uuid.UUID, namespace: str) -> str:
        return cls.tenant(tenant_id, namespace, "*")

    @classmethod
    def idempotency(cls, key: str) -> str:
        return cls.global_("idem", key)

    @classmethod
    def token_denylist(cls, jti: str) -> str:
        return cls.global_("denylist", jti)

    @classmethod
    def rate_limit(cls, scope: str, identifier: str) -> str:
        return cls.global_("ratelimit", scope, identifier)


# --------------------------------------------------------------- operations


class Cache:
    """Cache-aside helpers that fail open.

    Read through the cache, write to Postgres, then invalidate. The cache is
    never a source of truth.
    """

    def __init__(self, client: Redis | None = None) -> None:
        self._redis = client or get_redis()

    async def get_json(self, key: str) -> Any | None:
        try:
            raw = await self._redis.get(key)
        except RedisError as exc:
            logger.warning(
                "cache read failed, falling through",
                extra={"cache_error": type(exc).__name__},
            )
            return None
        if raw is None:
            return None
        try:
            return json.loads(raw)
        except json.JSONDecodeError:
            # A corrupt entry is not worth failing a request over.
            await self.delete(key)
            return None

    async def set_json(self, key: str, value: Any, ttl_seconds: int) -> None:
        try:
            await self._redis.set(key, json.dumps(value, default=str), ex=ttl_seconds)
        except RedisError as exc:
            logger.warning("cache write failed", extra={"cache_error": type(exc).__name__})

    async def delete(self, *keys: str) -> None:
        if not keys:
            return
        try:
            await self._redis.delete(*keys)
        except RedisError as exc:
            logger.warning("cache delete failed", extra={"cache_error": type(exc).__name__})

    async def delete_prefix(self, pattern: str) -> None:
        """Invalidate a namespace.

        Uses SCAN rather than KEYS -- KEYS blocks the Redis event loop, which at
        any real size stalls every other client.
        """
        try:
            async for key in self._redis.scan_iter(match=pattern, count=200):
                await self._redis.delete(key)
        except RedisError as exc:
            logger.warning("cache prefix delete failed", extra={"cache_error": type(exc).__name__})

    async def get_or_set(
        self,
        key: str,
        ttl_seconds: int,
        loader: Callable[[], Awaitable[T]],
    ) -> T:
        cached = await self.get_json(key)
        if cached is not None:
            return cached  # type: ignore[no-any-return]
        value = await loader()
        await self.set_json(key, value, ttl_seconds)
        return value

    async def ping(self) -> bool:
        try:
            return bool(await self._redis.ping())
        except RedisError:
            return False


async def check_redis() -> dict[str, Any]:
    """Readiness probe. Never raises."""
    try:
        await get_redis().ping()
        return {"status": "ok"}
    except Exception as exc:
        return {"status": "error", "error": type(exc).__name__}


# Default TTLs, from docs/09-logging-and-caching.md
class TTL:
    REFERENCE_DATA = 15 * 60
    DASHBOARD_COUNTS = 60
    IDEMPOTENCY = 24 * 60 * 60
