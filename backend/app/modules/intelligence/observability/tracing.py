"""LangSmith tracing, enabled by configuration alone.

## What this buys, and what it does not

LangChain and LangGraph emit traces to LangSmith when a handful of environment
variables are set — there is no code to write per call site. What a trace gives
you that a log line does not is the **tree**: which node ran, in what order,
with what state, how long each took, and the exact prompt and response at each
LLM boundary. For a graph with a conditional cycle, "did the rewrite fire, and
what did it change the query to" is one click instead of a log-grepping
exercise.

**The honest limitation:** the local Ollama adapter talks raw HTTP, so its
calls do not appear as LLM spans. Only the graph structure and the Gemini calls
are traced. Wrapping Ollama in a LangChain chat model purely to get spans would
mean fighting that abstraction for the rest of its life (see the note in
``adapters/pinecone_store.py`` for the same trade made the other way), so the
node-level timings in the graph trace are what serves that purpose instead.

## Why it is off unless configured

A trace contains prompts, and prompts contain ticket text. Sending that to a
third-party service is a decision someone has to make deliberately, so the
default is off and turning it on is one environment variable. The PII redaction
in the input guardrail runs *before* the model call, so what a trace records is
already redacted — but that mitigates the exposure rather than removing it.
"""

from __future__ import annotations

import os

from app.core.config import settings
from app.core.logging import get_logger

logger = get_logger(__name__)


def configure_tracing() -> bool:
    """Set the environment variables LangChain reads. Returns whether it is on.

    Called once at startup. Writing to ``os.environ`` rather than passing a
    client around is how these libraries are designed to be configured; it is
    also why this needs to happen *before* the first LangChain object is built,
    which is why it runs in the lifespan rather than lazily.
    """
    if not settings.LANGSMITH_API_KEY:
        return False

    os.environ["LANGSMITH_TRACING"] = "true"
    os.environ["LANGSMITH_API_KEY"] = settings.LANGSMITH_API_KEY
    os.environ["LANGSMITH_PROJECT"] = settings.LANGSMITH_PROJECT
    if settings.LANGSMITH_ENDPOINT:
        os.environ["LANGSMITH_ENDPOINT"] = settings.LANGSMITH_ENDPOINT

    logger.info(
        "langsmith tracing enabled",
        extra={"project": settings.LANGSMITH_PROJECT},
    )
    return True
