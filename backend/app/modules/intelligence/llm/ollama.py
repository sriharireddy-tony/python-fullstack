"""Local chat model via Ollama.

The fallback for every role, and during development often the primary. Free,
private, and never rate-limited — which for a project on a 500-request daily
hosted quota is not a nicety.

Slower and weaker than Gemini at reasoning. That trade is acceptable precisely
because the pipeline is built so no AI call sits on a write path: a slow
suggestion panel is a slow suggestion panel, not a slow ticket.
"""

from __future__ import annotations

import json
from typing import Any

import httpx
from pydantic import BaseModel, ValidationError

from app.core.config import settings
from app.core.errors import ServiceUnavailableError
from app.core.logging import get_logger

logger = get_logger(__name__)

#: Ollama's default context window is small enough to silently truncate a
#: multi-candidate rerank prompt, and truncation shows up as a model that
#: "ignored half the candidates" rather than as an error. Set explicitly.
DEFAULT_CONTEXT_TOKENS = 8192


class OllamaChatModel:
    """Chat and structured output against a local Ollama model.

    Structured output uses Ollama's ``format`` parameter with a **JSON schema**,
    not a prompt asking politely for JSON. The difference matters: the schema
    constrains decoding, so the model cannot emit prose around the object or
    invent a field. A prompt instruction is a request; a schema is a
    constraint, and only one of them survives a model having a bad day.
    """

    def __init__(
        self,
        model: str | None = None,
        base_url: str | None = None,
        temperature: float = 0.0,
        timeout_seconds: int | None = None,
    ) -> None:
        self._model = model or settings.OLLAMA_CHAT_MODEL
        self._base_url = (base_url or settings.OLLAMA_BASE_URL).rstrip("/")
        self._temperature = temperature
        self._timeout = timeout_seconds or settings.LLM_TIMEOUT_SECONDS

    @property
    def model_name(self) -> str:
        return self._model

    #: Token counts from the last call, so the registry can account for real
    #: usage instead of estimating. Ollama reports these on every response;
    #: reading them is strictly better than four-characters-per-token, and it
    #: is the difference between cost figures that are approximately right and
    #: cost figures that drift as prompts change shape.
    last_input_tokens: int = 0
    last_output_tokens: int = 0

    async def generate(self, system: str, messages: list[dict[str, str]]) -> str:
        payload = self._payload(system, messages)
        data = await self._post(payload)
        return str(data.get("message", {}).get("content", ""))

    async def generate_structured(
        self,
        system: str,
        messages: list[dict[str, str]],
        schema: type,
    ) -> Any:
        """Return a validated instance of ``schema``.

        Two layers, because constrained decoding is not a guarantee: the schema
        is passed to Ollama *and* the response is validated with Pydantic. If
        validation fails the caller gets an exception rather than a
        half-populated object, because a partially-parsed judgement quietly
        becomes a wrong suggestion shown to a human.
        """
        if not (isinstance(schema, type) and issubclass(schema, BaseModel)):
            raise TypeError("generate_structured requires a Pydantic model")

        payload = self._payload(system, messages)
        payload["format"] = schema.model_json_schema()
        data = await self._post(payload)
        content = str(data.get("message", {}).get("content", "")).strip()

        try:
            return schema.model_validate_json(content)
        except ValidationError as exc:
            # Log a bounded excerpt: the raw output is model-generated text and
            # could be arbitrarily long, and the first 400 characters are what
            # make the failure diagnosable.
            logger.warning(
                "structured output failed validation",
                extra={"chat_model": self._model, "excerpt": content[:400]},
            )
            raise ServiceUnavailableError("The model returned an unusable response.") from exc

    def _payload(self, system: str, messages: list[dict[str, str]]) -> dict[str, Any]:
        return {
            "model": self._model,
            "messages": [{"role": "system", "content": system}, *messages],
            "stream": False,
            # Chain-of-thought off.
            #
            # `qwen3` is a reasoning model: left to itself it emits a long
            # `thinking` block before the answer. Measured on this machine,
            # "Say OK" produced 9 output tokens without it and 217 with it --
            # and the cost of a call here is dominated by output tokens. For a
            # ten-candidate rerank that difference is the gap between an
            # interactive response and a 90-second wait.
            #
            # It is a real trade, not a free win: reasoning can improve a
            # borderline classification. The reason it is off by default is
            # that the output is schema-constrained anyway, so the model cannot
            # reason *in* its answer, and the visible thinking is thrown away.
            # Paying twenty times the tokens for text nobody reads is the wrong
            # side of the trade. Enable `OLLAMA_THINKING` and raise the rerank
            # deadline if quality on hard cases matters more than latency.
            "think": settings.OLLAMA_THINKING,
            "options": {
                # Zero temperature so a rerun of the same prompt gives the same
                # answer. Non-determinism here would make every cached result
                # and every eval number unreproducible.
                "temperature": self._temperature,
                "num_ctx": DEFAULT_CONTEXT_TOKENS,
            },
        }

    async def _post(self, payload: dict[str, Any]) -> dict[str, Any]:
        try:
            async with httpx.AsyncClient(timeout=httpx.Timeout(self._timeout)) as client:
                response = await client.post(f"{self._base_url}/api/chat", json=payload)
                response.raise_for_status()
                data = dict(response.json())
                self.last_input_tokens = int(data.get("prompt_eval_count") or 0)
                self.last_output_tokens = int(data.get("eval_count") or 0)
                return data
        except httpx.TimeoutException as exc:
            # Distinguished from other transport errors in the message, because
            # the operator response differs: a timeout on a local model means
            # "the deadline is too short for this hardware", not "Ollama is
            # down".
            logger.warning(
                "local model exceeded its deadline",
                extra={"chat_model": self._model, "timeout_seconds": self._timeout},
            )
            raise ServiceUnavailableError("The local model did not answer in time.") from exc
        except httpx.HTTPError as exc:
            raise ServiceUnavailableError("The local model is unavailable.") from exc

    async def health(self) -> bool:
        try:
            async with httpx.AsyncClient(timeout=5.0) as client:
                response = await client.get(f"{self._base_url}/api/tags")
                if response.status_code != 200:
                    return False
                names = {m.get("name", "") for m in response.json().get("models", [])}
                # A model that is not pulled fails on first use with a 404 that
                # looks like an outage. Better to report it as not ready.
                return any(name.split(":")[0] == self._model.split(":")[0] for name in names)
        except Exception:
            return False


def token_estimate(text: str) -> int:
    """Rough token count for usage accounting.

    Ollama does not return token counts for every call shape, and an estimate
    that is consistently within ~20% is enough for the thing this number is
    for: comparing the cost of features against each other. Four characters
    per token is the usual English approximation.
    """
    return max(1, len(text) // 4)


def json_excerpt(value: Any, limit: int = 2000) -> str:
    """Compact JSON for prompt construction, bounded."""
    return json.dumps(value, separators=(",", ":"), default=str)[:limit]
