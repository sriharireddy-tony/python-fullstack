"""Hosted chat model via Gemini.

Uses ``langchain_google_genai`` rather than the raw SDK, so LangChain's
structured-output machinery (``with_structured_output``) does the schema
plumbing. That is a genuine convenience here: it negotiates function-calling
versus JSON-mode per model, which is exactly the kind of provider detail this
adapter exists to absorb.

The import is deferred into the constructor. With no API key configured this
adapter is never built, and the AI layer must work on a machine where
``langchain-google-genai`` was never installed — the dependency group is
optional on purpose.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel

from app.core.config import settings
from app.core.errors import RateLimitError, ServiceUnavailableError
from app.core.logging import get_logger

logger = get_logger(__name__)

#: Substrings that mark a quota or rate-limit refusal rather than a real
#: failure. Matched on the message because the provider surfaces these as
#: generic exceptions through several layers of wrapper, and the distinction
#: decides whether we fall back to the local model or fail the request.
_QUOTA_MARKERS = ("429", "quota", "rate limit", "resource_exhausted", "exhausted")


class GeminiChatModel:
    """Gemini via LangChain.

    Temperature is pinned to 0. Every call this model serves is a
    *classification* — is this a duplicate, is this query good enough — and a
    classification that changes between identical runs cannot be cached,
    evaluated, or debugged.
    """

    def __init__(
        self,
        model: str | None = None,
        temperature: float = 0.0,
        timeout_seconds: int | None = None,
    ) -> None:
        if not settings.GOOGLE_API_KEY:
            raise ServiceUnavailableError("GOOGLE_API_KEY is not configured.")

        try:
            from langchain_google_genai import ChatGoogleGenerativeAI
        except ImportError as exc:  # pragma: no cover - optional dependency
            raise ServiceUnavailableError(
                "langchain-google-genai is not installed; install the 'ai' extra."
            ) from exc

        self._model = model or settings.GEMINI_MODEL_STRONG
        self._client = ChatGoogleGenerativeAI(
            model=self._model,
            google_api_key=settings.GOOGLE_API_KEY,
            temperature=temperature,
            timeout=timeout_seconds or settings.LLM_TIMEOUT_SECONDS,
            # One retry, for a transient network fault only. Retrying a quota
            # refusal is pointless -- the fallback chain handles that, and a
            # retry there just spends the caller's latency budget confirming a
            # 429. Retry and fallback are different mechanisms for different
            # failures, and conflating them is how a rate limit turns into a
            # thirty-second hang.
            max_retries=1,
        )

    @property
    def model_name(self) -> str:
        return self._model

    async def generate(self, system: str, messages: list[dict[str, str]]) -> str:
        payload = self._to_langchain(system, messages)
        try:
            result = await self._client.ainvoke(payload)
        except Exception as exc:
            raise self._translate(exc) from exc
        return _as_text(getattr(result, "content", ""))

    async def generate_structured(
        self,
        system: str,
        messages: list[dict[str, str]],
        schema: type,
    ) -> Any:
        if not (isinstance(schema, type) and issubclass(schema, BaseModel)):
            raise TypeError("generate_structured requires a Pydantic model")

        payload = self._to_langchain(system, messages)
        try:
            structured = self._client.with_structured_output(schema)
            return await structured.ainvoke(payload)
        except Exception as exc:
            raise self._translate(exc) from exc

    def _to_langchain(self, system: str, messages: list[dict[str, str]]) -> list[tuple[str, str]]:
        """LangChain accepts (role, content) tuples, which keeps the port's
        plain-dict message shape from leaking provider types upward."""
        return [("system", system), *[(m["role"], m["content"]) for m in messages]]

    def _translate(self, exc: Exception) -> Exception:
        """Turn a provider exception into one of ours.

        The distinction that matters: a quota refusal is a `RateLimitError`,
        which the registry answers by falling back to the local model. Anything
        else is a `ServiceUnavailableError`, which is not worth falling back
        for because it will usually fail the same way twice.
        """
        text = str(exc).lower()
        if any(marker in text for marker in _QUOTA_MARKERS):
            logger.warning("gemini quota exhausted", extra={"chat_model": self._model})
            return RateLimitError("The hosted model's quota is exhausted.")
        logger.warning(
            "gemini call failed",
            extra={"chat_model": self._model, "error": type(exc).__name__},
        )
        return ServiceUnavailableError("The hosted model is unavailable.")

    async def health(self) -> bool:
        return bool(settings.GOOGLE_API_KEY)


def _as_text(content: Any) -> str:
    """Flatten a message's content to plain text.

    Gemini 3.x returns **content blocks**, not a string::

        [{"type": "text", "text": "The answer...", "extras": {...}}]

    `str()` on that produces a Python repr, and the first chat turn duly showed
    a user a JSON-ish blob with a signature field in it. Older models returned
    a bare string, which is why the naive version looked correct in testing
    against the rerank path -- that path uses structured output and never
    touches this method.

    Handles all three shapes, because which one arrives depends on the model
    and the provider will add more.
    """
    if isinstance(content, str):
        return content.strip()
    if isinstance(content, list):
        parts: list[str] = []
        for block in content:
            if isinstance(block, str):
                parts.append(block)
            elif (
                isinstance(block, dict)
                # Only text blocks. A thinking or tool block is not the answer,
                # and concatenating one into the reply is how a model's internal
                # monologue ends up in front of a user.
                and block.get("type") in (None, "text")
                and block.get("text")
            ):
                parts.append(str(block["text"]))
        return "\n".join(parts).strip()
    return str(content).strip()
