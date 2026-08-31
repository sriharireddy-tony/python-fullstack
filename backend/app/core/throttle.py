"""Token denylist and rate limiting, with an in-process fallback.

Both need a shared counter, and both must keep working when Redis is not
available -- Redis is optional in development and can fail in production.

The fallback is a bounded in-process store. On a single application instance it
is fully correct. Across several instances it degrades to per-instance, which is
weaker but still far better than the alternatives:

* For the **denylist**, failing open would leave a logged-out access token
  usable for its full lifetime. Failing fully closed would sign everyone out
  during a cache blip. The in-process set means logout works correctly on the
  instance that served it, and the damage window elsewhere stays bounded by the
  15-minute access-token TTL.

* For **rate limiting**, failing open turns a Redis outage into an open
  brute-force window on the login endpoint. That is the one place where
  availability is not the priority.

See docs/09-logging-and-caching.md.
"""

from __future__ import annotations

import contextlib
import time
from dataclasses import dataclass, field

from redis.exceptions import RedisError

from app.core.cache import CacheKey, get_redis
from app.core.logging import get_logger

logger = get_logger(__name__)

#: Cap on the in-process store, so a flood of distinct keys cannot exhaust
#: memory while Redis is unavailable.
MAX_LOCAL_ENTRIES = 50_000


@dataclass
class _LocalTTLStore:
    """Minimal expiring key/counter store.

    Swept lazily on write rather than by a background task -- there is no
    scheduler in this application, and a timer thread would be more machinery
    than the problem deserves.
    """

    _expiry: dict[str, float] = field(default_factory=dict)
    _counts: dict[str, int] = field(default_factory=dict)

    def _sweep(self) -> None:
        now = time.monotonic()
        expired = [key for key, at in self._expiry.items() if at <= now]
        for key in expired:
            self._expiry.pop(key, None)
            self._counts.pop(key, None)

        if len(self._expiry) > MAX_LOCAL_ENTRIES:
            # Drop the soonest-to-expire first; they are least useful to keep.
            for key, _ in sorted(self._expiry.items(), key=lambda kv: kv[1])[
                : len(self._expiry) - MAX_LOCAL_ENTRIES
            ]:
                self._expiry.pop(key, None)
                self._counts.pop(key, None)

    def add(self, key: str, ttl_seconds: int) -> None:
        self._sweep()
        self._expiry[key] = time.monotonic() + ttl_seconds

    def contains(self, key: str) -> bool:
        at = self._expiry.get(key)
        if at is None:
            return False
        if at <= time.monotonic():
            self._expiry.pop(key, None)
            self._counts.pop(key, None)
            return False
        return True

    def increment(self, key: str, ttl_seconds: int) -> int:
        self._sweep()
        if not self.contains(key):
            self._expiry[key] = time.monotonic() + ttl_seconds
            self._counts[key] = 0
        self._counts[key] = self._counts.get(key, 0) + 1
        return self._counts[key]

    def reset(self, key: str) -> None:
        self._expiry.pop(key, None)
        self._counts.pop(key, None)


_local = _LocalTTLStore()


# --------------------------------------------------------------- denylist


class TokenDenylist:
    """Revoked access tokens, by ``jti``.

    A signed JWT is valid until it expires, so logout and deactivation need
    somewhere to record "this specific token is finished".
    """

    async def add(self, jti: str, ttl_seconds: int) -> None:
        key = CacheKey.token_denylist(jti)
        # Always record locally, so this instance is correct even if the shared
        # write below fails.
        _local.add(key, ttl_seconds)
        try:
            await get_redis().set(key, "1", ex=ttl_seconds)
        except RedisError as exc:
            logger.warning(
                "denylist write failed; falling back to in-process only",
                extra={"cache_error": type(exc).__name__},
            )

    async def contains(self, jti: str) -> bool:
        key = CacheKey.token_denylist(jti)
        if _local.contains(key):
            return True
        try:
            return await get_redis().exists(key) > 0
        except RedisError as exc:
            logger.warning(
                "denylist read failed; relying on the in-process copy",
                extra={"cache_error": type(exc).__name__},
            )
            return False


denylist = TokenDenylist()


# ----------------------------------------------------------- rate limiting


@dataclass(frozen=True, slots=True)
class RateLimit:
    """A fixed-window limit."""

    limit: int
    window_seconds: int


#: Login is the only endpoint exposed to unauthenticated traffic, so it is
#: where brute force lands. Limited per account *and* per IP: per-account alone
#: lets an attacker spray many accounts from one host; per-IP alone lets a
#: distributed attacker grind one account.
LOGIN_PER_ACCOUNT = RateLimit(limit=10, window_seconds=300)
LOGIN_PER_IP = RateLimit(limit=30, window_seconds=300)
REFRESH_PER_ACCOUNT = RateLimit(limit=60, window_seconds=300)


class RateLimiter:
    async def hit(self, scope: str, identifier: str, rule: RateLimit) -> tuple[bool, int]:
        """Record an attempt. Returns ``(allowed, current_count)``."""
        key = CacheKey.rate_limit(scope, identifier)
        try:
            redis = get_redis()
            count = int(await redis.incr(key))
            if count == 1:
                await redis.expire(key, rule.window_seconds)
        except RedisError as exc:
            logger.warning(
                "rate limiter falling back to in-process counters",
                extra={"cache_error": type(exc).__name__},
            )
            count = _local.increment(key, rule.window_seconds)

        return count <= rule.limit, count

    async def reset(self, scope: str, identifier: str) -> None:
        """Clear the counter, called after a successful login."""
        key = CacheKey.rate_limit(scope, identifier)
        _local.reset(key)
        with contextlib.suppress(RedisError):
            await get_redis().delete(key)


rate_limiter = RateLimiter()


# ------------------------------------------------------------ idempotency


class IdempotencyStore:
    """Remembers what a given Idempotency-Key already produced.

    CS agents double-click. Without this, a retried or double-submitted create
    produces a second ticket that then has to be found and cleaned up -- and
    with a client on the phone, nobody notices until later.

    Only the resulting id is stored, not the response body: the caller reloads
    the record, so the reply is always current rather than a replay of stale
    JSON.
    """

    TTL_SECONDS = 24 * 60 * 60

    def _key(self, scope: str, key: str) -> str:
        return CacheKey.idempotency(f"{scope}:{key}")

    async def get(self, scope: str, key: str) -> str | None:
        full = self._key(scope, key)
        try:
            value = await get_redis().get(full)
            if value is not None:
                return str(value)
        except RedisError as exc:
            logger.warning(
                "idempotency read failed; using the in-process copy",
                extra={"cache_error": type(exc).__name__},
            )
        return _local_values.get(full) if _local.contains(full) else None

    async def set(self, scope: str, key: str, value: str) -> None:
        full = self._key(scope, key)
        _local.add(full, self.TTL_SECONDS)
        _local_values[full] = value
        try:
            await get_redis().set(full, value, ex=self.TTL_SECONDS)
        except RedisError as exc:
            logger.warning(
                "idempotency write failed; in-process only",
                extra={"cache_error": type(exc).__name__},
            )


#: Values for the in-process idempotency fallback. Kept beside _local, whose
#: expiry entries govern their lifetime.
_local_values: dict[str, str] = {}

idempotency = IdempotencyStore()
