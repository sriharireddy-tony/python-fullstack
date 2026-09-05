"""Model registry: roles to models, with fallbacks and budgets.

## Why a registry rather than constructing a model where it is used

Because the interesting questions are all *per role*, not per call site:

* The reranker needs a capable model; the query rewriter does not. Both are
  "the LLM" if you construct a client inline, and then you cannot make that
  trade without editing node code.
* When the hosted quota runs out mid-afternoon, every role should degrade
  according to its own tolerance. The evaluator must **never** silently fall
  back to a weaker model, because an evaluation whose judge changed halfway
  through a run is worse than no evaluation.
* Cost attribution has to be per feature, or "the AI costs too much" is not a
  question anyone can answer.

So each role is declared once, here, with its model, its fallback, and its
budget. This is also the **only file in the module that knows a model name** —
enforced by a CI grep. Everything else asks for a role.
"""

from __future__ import annotations

import time
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from enum import StrEnum
from typing import Any, TypeVar

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.errors import RateLimitError, ServiceUnavailableError
from app.core.logging import get_logger
from app.modules.intelligence.ports import ChatModel
from app.modules.intelligence.repository import UsageRepository

logger = get_logger(__name__)

T = TypeVar("T")


class ModelRole(StrEnum):
    """What a model is being asked to do.

    Roles, not model names, are what the rest of the code refers to. Swapping
    which model serves a role is then a configuration change rather than a code
    change — and the ability to do that in one place was an explicit
    requirement.
    """

    #: Classifies candidates as duplicate / related / recurring. The most
    #: quality-sensitive call in the system, because a wrong answer is shown to
    #: a human as a confident judgement.
    RERANK = "rerank"
    #: Rewrites a weak query. Cheap, forgiving, high volume.
    QUERY_REWRITE = "query_rewrite"
    #: The conversational assistant.
    CHAT = "chat"
    #: The analysis agent's reasoning loop.
    AGENT = "agent"
    #: Grades the system's own output during evaluation. Never falls back.
    EVALUATOR = "evaluator"


@dataclass(frozen=True, slots=True)
class RoleConfig:
    """How one role is served."""

    role: ModelRole
    #: Preferred model, hosted where a key exists.
    primary: str
    #: Ordered fallbacks, tried in turn when the one before is rate-limited
    #: or unreachable. Empty means fail instead — the correct answer for the
    #: evaluator.
    #:
    #: A chain rather than a single fallback because the observed failure
    #: demanded it: the strong hosted model's free-tier quota exhausted within
    #: a day, and a one-step fallback dropped the agent straight from a hosted
    #: frontier model to a local 8B. The cheap hosted model sits between them
    #: and is a far smaller capability drop — measured *better* than the strong
    #: one at reranking, and four times faster. Degrading one step at a time
    #: rather than falling off a cliff.
    fallbacks: tuple[str, ...]
    #: Our own daily ceiling on the primary, checked before calling. The
    #: provider's counter is invisible to us, so this is how exhaustion is
    #: anticipated rather than discovered through a 429 mid-run.
    daily_limit: int
    #: Rough cost per thousand tokens, for attribution. Zero for local models,
    #: which is the honest number and also the reason development uses them.
    cost_per_1k: float = 0.0
    #: Per-role deadline for one call.
    #:
    #: Set per role rather than globally because the roles have different
    #: users. A rerank happens while somebody watches a page load, so it gets a
    #: deadline short enough that the page stays a page. The agent in Phase E
    #: runs in a worker with nobody waiting, so it can afford minutes.
    #:
    #: This is measured, not guessed: the local 8B model takes ~60-90s to judge
    #: twenty candidates, which is not an HTTP response. Cutting it off and
    #: showing the fused order is a better answer than a spinner that
    #: eventually fails -- and with a hosted model the same call is 2-4s, well
    #: inside the deadline, so this only bites the development configuration.
    timeout_seconds: int = 30


def _roles() -> dict[ModelRole, RoleConfig]:
    """Build the role table from settings.

    A function rather than a module constant so settings are read at call time;
    a constant would freeze whatever the environment looked like at import.
    """
    hosted_strong = settings.GEMINI_MODEL_STRONG
    hosted_cheap = settings.GEMINI_MODEL_CHEAP
    local = settings.OLLAMA_CHAT_MODEL

    #: hosted-cheap first, then local. The local model is last because it is
    #: the only one that is free but also the slowest and weakest; anything
    #: hosted is preferable while quota remains.
    chain: tuple[str, ...] = (hosted_cheap,)
    if settings.AI_LOCAL_FALLBACK:
        chain = (*chain, local)

    return {
        # 500 hosted requests a day is the free-tier budget, and the reranker is
        # the call worth spending it on. Deliberately under the real ceiling so
        # the chatbot is not starved by a busy afternoon of ticket triage.
        #
        # The **cheap** model, and that was a surprise. This role was written
        # with the strong model on the assumption that classification is the
        # quality-sensitive call. Measured over twelve golden queries:
        #
        #   | model                   | precision@5 | s/query |
        #   |-------------------------|-------------|---------|
        #   | retrieval only          | 0.633       | 0.8     |
        #   | gemini-3.5-flash        | 0.912       | 16.7    |
        #   | gemini-flash-lite-latest| 0.963       | 4.2     |
        #
        # The cheap model was better *and* four times faster. The precision gap
        # is within noise on a twelve-query sample; the latency gap is not, and
        # 4s is the difference between a panel that loads with the page and one
        # people watch a spinner for. Changing this was one line, which is the
        # entire argument for the registry existing.
        ModelRole.RERANK: RoleConfig(
            ModelRole.RERANK,
            hosted_cheap,
            # Already the cheap model, so the chain is just the local fallback.
            (local,) if settings.AI_LOCAL_FALLBACK else (),
            daily_limit=250,
            cost_per_1k=0.0001,
            # Someone is watching a page. Past this the fused order is the
            # better answer, and the trace records that it was cut off.
            timeout_seconds=settings.RERANK_TIMEOUT_SECONDS,
        ),
        # Rewriting is a one-sentence transformation. Paying strong-model rates
        # for it would be spending the quota on the least valuable call.
        ModelRole.QUERY_REWRITE: RoleConfig(
            ModelRole.QUERY_REWRITE,
            hosted_cheap,
            (local,) if settings.AI_LOCAL_FALLBACK else (),
            daily_limit=100,
            cost_per_1k=0.0001,
            # A rewrite is one short sentence, and it happens *before* the
            # rerank on the same request, so its budget has to leave room.
            timeout_seconds=settings.REWRITE_TIMEOUT_SECONDS,
        ),
        ModelRole.CHAT: RoleConfig(
            ModelRole.CHAT, hosted_strong, chain, daily_limit=150, cost_per_1k=0.0003
        ),
        ModelRole.AGENT: RoleConfig(
            ModelRole.AGENT,
            hosted_strong,
            chain,
            daily_limit=100,
            cost_per_1k=0.0003,
            # Runs in a worker with nobody waiting, so it can afford to think.
            timeout_seconds=settings.LLM_TIMEOUT_SECONDS,
        ),
        # No fallback, by design. If the judge changes mid-run the numbers
        # before and after are not comparable, and a quiet downgrade would
        # produce a chart that looks like a regression in the system under
        # test. Better to stop and say the evaluation cannot run.
        ModelRole.EVALUATOR: RoleConfig(
            ModelRole.EVALUATOR, hosted_strong, (), daily_limit=200, cost_per_1k=0.0003
        ),
    }


def _is_hosted(model_name: str) -> bool:
    return model_name.startswith("gemini")


def _serveable(model_name: str) -> bool:
    """Whether this model could answer at all, without calling it.

    Checked before attempting, so a missing API key is *skipped* rather than
    attempted-and-failed. The difference shows up in the usage table: without
    this, every request records a failed hosted call it never actually made,
    and the failure rate for that model becomes fiction.
    """
    return bool(settings.GOOGLE_API_KEY) if _is_hosted(model_name) else True


def _build(model_name: str, timeout_seconds: int) -> ChatModel:
    """Construct the adapter for a model name.

    The one place a model name maps to a vendor. Local names are Ollama tags;
    anything else is treated as hosted.
    """
    if _is_hosted(model_name):
        from app.modules.intelligence.llm.gemini import GeminiChatModel

        return GeminiChatModel(model_name, timeout_seconds=timeout_seconds)

    from app.modules.intelligence.llm.ollama import OllamaChatModel

    return OllamaChatModel(model_name, timeout_seconds=timeout_seconds)


@dataclass(slots=True)
class CallResult:
    """The value, plus what producing it cost."""

    value: Any
    model: str
    latency_ms: int
    input_tokens: int
    output_tokens: int
    estimated_cost: float
    #: True when the primary was unavailable and the fallback answered. Carried
    #: to the caller because a result from a weaker model may deserve a lower
    #: confidence floor.
    used_fallback: bool


class ModelRegistry:
    """Resolves roles to models, records usage, and enforces our own budget.

    Session-bound because usage is recorded in the caller's transaction: an AI
    call that happened but was not accounted for is how cost tracking silently
    becomes fiction.
    """

    def __init__(self, session: AsyncSession, tenant_id: uuid.UUID | None = None) -> None:
        self._session = session
        self._tenant_id = tenant_id
        self._usage = UsageRepository(session)
        self._roles = _roles()

    def config(self, role: ModelRole) -> RoleConfig:
        return self._roles[role]

    async def available(self, role: ModelRole) -> bool:
        """Whether this role can serve a call at all right now."""
        config = self._roles[role]
        if await self._over_budget(config):
            return bool(config.fallbacks)
        return True

    async def _over_budget(self, config: RoleConfig) -> bool:
        # Counted per role, not per model: two roles share the cheap hosted
        # model, and a shared counter means a busy reranker spends the query
        # rewriter's budget. See `calls_today`.
        used = await self._usage.calls_today(config.primary, role=config.role.value)
        return used >= config.daily_limit

    async def _tenant_over_budget(self) -> bool:
        """Whether this tenant has spent its monthly allowance.

        A second, independent ceiling, and the one the per-model daily limits
        cannot express. Those cap how often *a model* is called across the
        whole installation; this caps what *one tenant* costs. In a
        multi-tenant product they fail differently: without a per-tenant cap,
        one busy workspace can consume the entire day's quota and every other
        tenant gets the degraded path without ever having used the feature.

        Zero disables it, which is the sensible default for a single-tenant
        deployment where the global limits already say everything.
        """
        if settings.AI_TENANT_MONTHLY_COST_CAP <= 0 or self._tenant_id is None:
            return False
        spent = await self._usage.month_cost(self._tenant_id)
        if spent < settings.AI_TENANT_MONTHLY_COST_CAP:
            return False
        logger.warning(
            "tenant over its monthly AI budget",
            extra={
                "tenant_id": str(self._tenant_id),
                "spent": round(spent, 4),
                "cap": settings.AI_TENANT_MONTHLY_COST_CAP,
            },
        )
        return True

    async def call(
        self,
        role: ModelRole,
        feature: str,
        run: Callable[[ChatModel], Awaitable[T]],
        *,
        prompt_text: str = "",
    ) -> CallResult:
        """Run ``run`` against the model for ``role``, recording what it cost.

        The callable takes the model rather than this method taking a prompt,
        so a caller can use ``generate`` or ``generate_structured`` without the
        registry needing to know which. The registry's concern is *which model,
        with what budget, accounted how* — not what is being asked.

        Fallback happens on ``RateLimitError`` and ``ServiceUnavailableError``
        only. Anything else is a bug in the caller and should surface, not be
        retried against a different model until something appears to work.
        """
        config = self._roles[role]
        attempts: list[str] = []

        # A tenant over its monthly allowance skips every *paid* model and
        # degrades to the local one. Degrading rather than refusing, because
        # the local model costs nothing and a feature that stops working
        # entirely on the last day of the month is worse than one that gets
        # slower.
        tenant_capped = await self._tenant_over_budget()

        if tenant_capped:
            attempts = [name for name in config.fallbacks if not _is_hosted(name)]
            if not attempts:
                raise RateLimitError(
                    "This workspace has reached its monthly AI budget. It resets next month."
                )
            logger.info(
                "tenant budget reached, using the local model",
                extra={"role": role.value, "model": attempts[0]},
            )
            return await self._attempt(config, attempts, feature, run, prompt_text)

        if await self._over_budget(config):
            logger.info(
                "role over its daily budget, skipping primary",
                extra={"role": role.value, "model": config.primary},
            )
        else:
            attempts.append(config.primary)

        for fallback in config.fallbacks:
            if fallback not in attempts:
                attempts.append(fallback)

        # Drop anything that cannot be served -- a hosted model with no key
        # configured is not an attempt, it is a misconfiguration, and treating
        # it as an attempt pollutes the failure metrics for that model.
        attempts = [name for name in attempts if _serveable(name)]

        if not attempts:
            raise RateLimitError(
                f"The {role.value} model is over its daily budget and has no fallback."
            )

        return await self._attempt(config, attempts, feature, run, prompt_text)

    async def _attempt(
        self,
        config: RoleConfig,
        attempts: list[str],
        feature: str,
        run: Callable[[ChatModel], Awaitable[T]],
        prompt_text: str,
    ) -> CallResult:
        """Try each model in turn, recording what each attempt cost.

        Extracted so the budget paths and the normal path share one
        implementation. Two copies of a fallback loop is two places for the
        usage accounting to drift.
        """
        last_error: Exception | None = None
        for index, model_name in enumerate(attempts):
            started = time.perf_counter()
            try:
                model = _build(model_name, config.timeout_seconds)
                value = await run(model)
            except (RateLimitError, ServiceUnavailableError) as exc:
                last_error = exc
                await self._record(config, model_name, feature, 0, 0, 0, succeeded=False)
                logger.warning(
                    "model call failed, trying next",
                    extra={
                        "role": config.role.value,
                        "model": model_name,
                        "error": type(exc).__name__,
                        "remaining": len(attempts) - index - 1,
                    },
                )
                continue

            latency_ms = int((time.perf_counter() - started) * 1000)
            # Real counts when the adapter reports them, an estimate otherwise.
            # Asked of the model object rather than declared on the port,
            # because not every provider returns them and a port method that
            # half the implementations cannot honour is worse than a getattr.
            input_tokens = int(getattr(model, "last_input_tokens", 0)) or _estimate(prompt_text)
            output_tokens = int(getattr(model, "last_output_tokens", 0)) or _estimate(str(value))
            # Priced by the model that actually answered, not by the role's
            # rate. The rate belongs to the role's *primary*; charging it for a
            # local fallback invents money. The usage report showed the local
            # 8B model billed $0.005 for reranking, which is a number nobody
            # paid -- and cost-per-accepted-suggestion is only meaningful if
            # the costs in it are real.
            cost = (
                0.0
                if not _is_hosted(model_name)
                else (input_tokens + output_tokens) / 1000 * config.cost_per_1k
            )
            await self._record(
                config,
                model_name,
                feature,
                input_tokens,
                output_tokens,
                latency_ms,
                succeeded=True,
                cost=cost,
            )
            return CallResult(
                value=value,
                model=model_name,
                latency_ms=latency_ms,
                input_tokens=input_tokens,
                output_tokens=output_tokens,
                estimated_cost=cost,
                used_fallback=index > 0,
            )

        raise last_error or ServiceUnavailableError("No model could serve this request.")

    async def _record(
        self,
        config: RoleConfig,
        model: str,
        feature: str,
        input_tokens: int,
        output_tokens: int,
        latency_ms: int,
        *,
        succeeded: bool,
        cost: float = 0.0,
    ) -> None:
        await self._usage.record(
            tenant_id=self._tenant_id,
            feature=feature,
            role=config.role.value,
            model=model,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            estimated_cost=cost,
            latency_ms=latency_ms or None,
            succeeded=succeeded,
        )


def _estimate(text: str) -> int:
    """Token estimate, used when the provider does not report counts.

    Consistently approximate beats occasionally exact here: this number exists
    to compare features against each other and to notice a prompt that has
    quietly tripled in size, and four characters per token is close enough for
    both.
    """
    return max(1, len(text) // 4)
