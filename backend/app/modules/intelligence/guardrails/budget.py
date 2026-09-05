"""Budget guardrails: the limits that stop a bug from becoming a bill.

Distinct from the other guardrails in what they protect against. Input and
output guards assume a *malicious* input; these assume an *ordinary* one and a
loop that does not stop — which in practice is the far more common incident.

Four independent limits, because each catches a failure the others miss:

| limit | catches |
|---|---|
| rate limit (per user) | one person refreshing a page in a tight loop |
| daily quota (per model) | the free tier running out mid-afternoon |
| turn cap | an agent that will not converge (Phase E) |
| wallclock | a model that hangs rather than fails |

Any one of them alone leaves a gap. A turn cap does not help if each turn takes
four minutes; a wallclock does not help if the loop is fast and infinite.
"""

from __future__ import annotations

import time
from dataclasses import dataclass

from app.core.logging import get_logger
from app.core.throttle import RateLimit, rate_limiter

logger = get_logger(__name__)


@dataclass(frozen=True, slots=True)
class BudgetDecision:
    allowed: bool
    reason: str = ""
    retry_after_seconds: int = 0


async def check_user_rate(
    user_id: str,
    feature: str,
    *,
    limit: int,
    window_seconds: int,
) -> BudgetDecision:
    """Per-user, per-feature rate limit.

    Keyed by user rather than by IP: everyone here is authenticated, and IP
    keying would throttle a whole office behind one NAT while doing nothing
    about a single account in a loop.

    Fails **open** on a Redis outage, which is the same choice the rest of the
    application makes for rate limiting. The reasoning: the downside of failing
    open is a burst of AI calls bounded by the daily model quota underneath;
    the downside of failing closed is the feature disappearing whenever Redis
    hiccups. The quota is the backstop that makes open acceptable here.
    """
    allowed, count = await rate_limiter.hit(
        f"ai:{feature}", user_id, RateLimit(limit=limit, window_seconds=window_seconds)
    )
    if allowed:
        return BudgetDecision(allowed=True)

    logger.info(
        "ai rate limit hit",
        extra={
            "feature": feature,
            "limit": limit,
            "window_seconds": window_seconds,
            "count": count,
        },
    )
    return BudgetDecision(
        allowed=False,
        reason="Too many AI requests. Try again shortly.",
        # A fixed window, so the honest worst case is the whole window. Telling
        # a client to retry sooner than that just produces another 429.
        retry_after_seconds=window_seconds,
    )


class Wallclock:
    """A deadline for one graph run.

    Checked between nodes rather than enforced with a timeout on the whole
    coroutine, so a run that is out of time finishes the node it is in and
    returns what it has. Cancelling mid-node would leave usage unrecorded and,
    for the agent, a half-written analysis row.
    """

    __slots__ = ("_budget", "_started")

    def __init__(self, seconds: float) -> None:
        self._started = time.monotonic()
        self._budget = seconds

    @property
    def elapsed(self) -> float:
        return time.monotonic() - self._started

    @property
    def remaining(self) -> float:
        return max(0.0, self._budget - self.elapsed)

    @property
    def expired(self) -> bool:
        return self.remaining <= 0.0

    def check(self, stage: str) -> BudgetDecision:
        if not self.expired:
            return BudgetDecision(allowed=True)
        logger.warning(
            "wallclock budget exhausted",
            extra={"stage": stage, "elapsed_seconds": round(self.elapsed, 1)},
        )
        return BudgetDecision(
            allowed=False,
            reason="The analysis ran out of time and returned what it had.",
        )
