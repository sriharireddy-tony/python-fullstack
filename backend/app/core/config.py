"""Application settings.

All configuration comes from the environment. Nothing is hardcoded, and the
application refuses to start when a required value is missing -- a misconfigured
app that boots and misbehaves is worse than one that will not boot at all.
"""

from __future__ import annotations

from functools import lru_cache
from typing import Annotated, Literal

from pydantic import Field, ValidationError, field_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict

Environment = Literal["local", "staging", "production"]
StorageBackend = Literal["local", "azure_blob"]
LogLevel = Literal["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    # --- general -------------------------------------------------------
    ENVIRONMENT: Environment = "local"
    APP_NAME: str = "OS Tracker"
    API_V1_PREFIX: str = "/api/v1"
    DEBUG: bool = False

    # --- database ------------------------------------------------------
    # The application connects as a role that owns nothing, so row-level
    # security is never bypassed. Alembic uses MIGRATION_DATABASE_URL, whose
    # role owns the schema. See docs/04-database.md.
    DATABASE_URL: str
    MIGRATION_DATABASE_URL: str | None = None
    DB_POOL_SIZE: int = 10
    DB_MAX_OVERFLOW: int = 10
    DB_ECHO: bool = False

    # --- cache ---------------------------------------------------------
    REDIS_URL: str = "redis://localhost:6379/0"

    # --- auth (used from Phase 1) --------------------------------------
    JWT_SECRET: str = Field(default="", min_length=0)
    JWT_ALGORITHM: str = "HS256"
    ACCESS_TOKEN_TTL_MINUTES: int = 15
    REFRESH_TOKEN_TTL_DAYS: int = 7
    COOKIE_SECURE: bool = True
    COOKIE_DOMAIN: str | None = None

    # --- storage -------------------------------------------------------
    STORAGE_BACKEND: StorageBackend = "local"
    STORAGE_LOCAL_PATH: str = "./var/uploads"
    MAX_UPLOAD_BYTES: int = 10 * 1024 * 1024
    MAX_FILES_PER_TICKET: int = 5

    # --- web -----------------------------------------------------------
    # NoDecode stops pydantic-settings from JSON-decoding the env value before
    # validation, so a plain comma-separated list works in .env as well as JSON.
    CORS_ORIGINS: Annotated[list[str], NoDecode] = ["http://localhost:5173"]

    # --- AI layer ------------------------------------------------------
    # Every AI capability is individually switchable. If one turns out weak in
    # practice it can be disabled without losing the others.
    AI_ENABLED: bool = True
    AI_SIMILARITY_ENABLED: bool = True
    #: Embed automatically when a ticket is created or its text changes.
    #:
    #: Off means nothing is embedded until someone asks for it from the
    #: embedding console. The outbox path is unchanged and still the only way
    #: work is queued -- this flag decides who pushes the button, not how the
    #: work happens. Turn it on and the system is back to embedding on write,
    #: which is what a production deployment should do.
    AI_AUTO_EMBED_ON_WRITE: bool = False
    AI_RERANK_ENABLED: bool = False  # opt-in: costs an LLM call per query
    AI_ANALYSIS_ENABLED: bool = True
    AI_CHAT_ENABLED: bool = True

    OLLAMA_BASE_URL: str = "http://localhost:11434"
    #: The 0.6B Qwen3 embedding model, which emits 1024 dimensions natively.
    #: The 8B tag ("qwen3-embedding") emits 4096 -- four times the storage and
    #: index memory for a quality difference this corpus cannot measure. The
    #: dimension is still probed from the model rather than configured, so
    #: changing this line is the only edit a model swap needs.
    OLLAMA_EMBED_MODEL: str = "qwen3-embedding:0.6b"
    OLLAMA_CHAT_MODEL: str = "qwen3:8b"
    #: Let the local reasoning model emit chain-of-thought before answering.
    #: Off by default: it costs ~20x the output tokens for text that is
    #: discarded, because the answer is schema-constrained anyway. See
    #: `adapters/ollama_chat.py`.
    OLLAMA_THINKING: bool = False

    #: Qwen3-Embedding is asymmetric: queries carry a task instruction,
    #: documents do not. Both strings are part of the embedding identity and go
    #: into source_hash, so changing either forces a re-embed rather than
    #: leaving a corpus half in each convention.
    EMBED_QUERY_INSTRUCTION: str = (
        "Instruct: Given a software bug report, retrieve other bug reports "
        "describing the same underlying problem\nQuery: "
    )
    EMBED_DOCUMENT_INSTRUCTION: str = ""
    EMBED_INSTRUCTION_VERSION: str = "v1"
    EMBED_BATCH_SIZE: int = 16
    EMBED_TITLE_MAX: int = 300
    EMBED_DESCRIPTION_MAX: int = 2000
    EMBED_STEPS_MAX: int = 1000

    # --- Pinecone ------------------------------------------------------
    #: Serverless, one index, one namespace per tenant. Pinecone bills per
    #: index, so index-per-tenant does not survive contact with a free tier;
    #: a namespace is a hard partition inside an index and costs nothing.
    PINECONE_API_KEY: str = ""
    PINECONE_INDEX: str = "os-tracker"
    PINECONE_CLOUD: str = "aws"
    PINECONE_REGION: str = "us-east-1"
    #: How long to wait for a newly created serverless index to become ready.
    PINECONE_READY_TIMEOUT: float = 60.0

    #: Over-fetch then truncate. Cheap insurance against a selective filter
    #: leaving too few results after post-filtering.
    RETRIEVAL_SEMANTIC_LIMIT: int = 50
    RETRIEVAL_LEXICAL_LIMIT: int = 50
    RETRIEVAL_CANDIDATES: int = 20
    RRF_K: int = 60
    #: How two rankings become one: "cascade", "weighted_rrf", or "rrf".
    #: Cascade ships because RRF was measured and lost -- `domain/fusion.py`
    #: carries the numbers and the derivation.
    FUSION_MODE: str = "cascade"
    RRF_SECONDARY_WEIGHT: float = 0.5
    #: Look literal identifiers up exactly and pin the unique matches.
    #: Measured to take the identifier query family from 0.417 recall to 1.000.
    EXACT_IDENTIFIER_PROBE: bool = True
    #: Below this fused score a candidate is not worth showing. A floor tuned to
    #: "always return something" is how this feature loses trust.
    SIMILARITY_FLOOR: float = 0.012
    RERANK_CONFIDENCE_FLOOR: float = 0.55
    #: Per-candidate description budget in the rerank prompt. Twenty candidates
    #: times an unbounded description overflows the context window, and the
    #: symptom is a model that appears to ignore half its input.
    RERANK_DESCRIPTION_MAX: int = 600
    #: Per-user rate limit on the similar-issues endpoint. Generous, because a
    #: cached page refresh should not hit it; the model quota is the real
    #: backstop.
    AI_RATE_LIMIT_PER_MINUTE: int = 20
    #: Analysis runs per user per hour. Much tighter than the similarity
    #: limit, because each run is several LLM calls rather than one cached
    #: lookup -- and on a 500-a-day quota, twenty runs is a meaningful
    #: fraction of the day's budget.
    AI_ANALYSIS_LIMIT_PER_HOUR: int = 10
    #: Chat turns per user per hour. Between the two other limits: a turn is
    #: one or two model calls, so more generous than an analysis run and
    #: tighter than a cached similarity lookup.
    AI_CHAT_LIMIT_PER_HOUR: int = 60
    #: Monthly hosted-model spend allowed per tenant, in the same units as
    #: `ai_usage.estimated_cost`. Zero disables the cap.
    #:
    #: Independent of the per-model daily limits, which cap a model across the
    #: whole installation. Without a per-tenant cap, one busy workspace can
    #: consume the entire day's quota and every other tenant silently gets the
    #: degraded path. Over the cap, a tenant keeps the feature on the local
    #: model rather than losing it.
    AI_TENANT_MONTHLY_COST_CAP: float = 5.0
    #: How long a similar-issues result is cached. The key already carries the
    #: ticket's version, so an edit invalidates it immediately -- this TTL is
    #: only there to bound how long a result computed against an *older corpus*
    #: survives, since a newly filed duplicate should not stay invisible.
    AI_CACHE_TTL_SECONDS: int = 900
    MAX_QUERY_REWRITES: int = 2

    GOOGLE_API_KEY: str = ""
    #: Model names verified against the live API, not assumed. `gemini-2.5-flash`
    #: and `gemini-2.5-flash-lite` both return 404 "no longer available to new
    #: users" on a key issued now -- which is exactly why the model registry
    #: exists: correcting this was a two-line configuration change rather than
    #: a hunt through call sites.
    #:
    #: Measured on the rerank prompt: `gemini-3.5-flash` ~9s, the lite models
    #: ~1s. Both inside the 25s interactive deadline, and both far ahead of the
    #: local 8B model's ~20s.
    #:
    #: `gemini-3.5-flash` is *not* the strong model here, despite being the
    #: stronger model. Its free-tier quota exhausted within a day of use
    #: (429 RESOURCE_EXHAUSTED), while the lite tier kept answering -- so on
    #: this plan the nominally weaker model is the one that is actually
    #: available, and an unavailable model is not a better model. Swap this
    #: back on a paid plan.
    GEMINI_MODEL_STRONG: str = "gemini-3.5-flash-lite"
    GEMINI_MODEL_CHEAP: str = "gemini-flash-lite-latest"
    #: Falls back to the local Ollama model when the hosted quota is gone, so
    #: development is never blocked. Never applied to the evaluator role.
    AI_LOCAL_FALLBACK: bool = True
    #: Serve every role from the local model, with the hosted ones as fallback.
    #:
    #: The inverse of the default. Off, hosted models lead and the local one
    #: catches quota exhaustion; on, nothing leaves the machine unless the
    #: local model fails outright.
    #:
    #: Three consequences worth knowing before turning it on:
    #:
    #: * **Cost goes to zero.** Nothing is billed.
    #: * **Latency roughly quadruples on rerank** -- measured 24.7s local
    #:   against 4.0s hosted, which is why `_timeout_for` below widens the
    #:   per-role deadlines when local leads.
    #: * **LangSmith stops showing prompts.** The Ollama adapter talks raw
    #:   HTTP rather than being a LangChain chat model, so its calls are not
    #:   LLM spans. Graph structure and node timings still trace; the prompt
    #:   and response text does not.
    AI_PREFER_LOCAL: bool = False
    #: Deadline for a role whose primary is the local model. The per-role
    #: timeouts below are sized for a hosted model answering in seconds; an 8B
    #: model on consumer hardware needs materially more, and a deadline that
    #: kills every call is worse than a slow one.
    OLLAMA_TIMEOUT_SECONDS: int = 180

    AGENT_MAX_TURNS: int = 8
    AGENT_TIMEOUT_SECONDS: int = 300
    AGENT_MAX_TOOL_CALLS: int = 24
    #: Cost ceiling for one analysis run, in the same units as `ai_usage`.
    #: Catches the failure the turn cap and the clock both miss: a loop that is
    #: cheap per turn and long.
    AGENT_MAX_COST: float = 0.05

    #: One call, one ceiling. A local 8B model on CPU is genuinely slow, so this
    #: is generous -- but unbounded would let one request hold a worker forever.
    LLM_TIMEOUT_SECONDS: int = 120
    #: Deadline for the reranker, which runs while someone waits for a page.
    #: A hosted model answers in 2-4s; a local 8B model takes 60-90s, so this
    #: cuts it off and the panel falls back to the fused order. Measured, not
    #: guessed -- see `registry.py`.
    RERANK_TIMEOUT_SECONDS: int = 25
    #: The rewrite happens before the rerank on the same request, so its
    #: deadline has to leave room for one.
    REWRITE_TIMEOUT_SECONDS: int = 12
    #: LangSmith. Off unless a key is set -- a trace carries prompt text, so
    #: sending it to a third party is a deliberate decision, not a default.
    LANGSMITH_API_KEY: str = ""
    LANGSMITH_PROJECT: str = "os-tracker"
    LANGSMITH_ENDPOINT: str = ""

    LLM_CACHE_PATH: str = "./var/llm_cache.db"
    CHAT_RETENTION_DAYS: int = 30

    # --- logging -------------------------------------------------------
    LOG_LEVEL: LogLevel = "INFO"
    LOG_JSON: bool = True

    @field_validator("CORS_ORIGINS", mode="before")
    @classmethod
    def _split_origins(cls, value: object) -> object:
        """Accept a comma-separated string as well as a JSON list."""
        if isinstance(value, str):
            text = value.strip()
            if text.startswith("["):
                import json

                return json.loads(text)
            return [item.strip() for item in text.split(",") if item.strip()]
        return value

    @property
    def is_production(self) -> bool:
        return self.ENVIRONMENT == "production"

    @property
    def migration_database_url(self) -> str:
        """Falls back to DATABASE_URL so local development works with one role."""
        return self.MIGRATION_DATABASE_URL or self.DATABASE_URL

    def assert_production_ready(self) -> None:
        """Guard rails that must hold before serving real traffic.

        Called during startup. Kept separate from field validation so local
        development is not burdened by production requirements.
        """
        if not self.is_production:
            return

        problems: list[str] = []
        if len(self.JWT_SECRET) < 32:
            problems.append("JWT_SECRET must be at least 32 characters in production")
        if not self.COOKIE_SECURE:
            problems.append("COOKIE_SECURE must be true in production")
        if self.DEBUG:
            problems.append("DEBUG must be false in production")
        if self.MIGRATION_DATABASE_URL is None:
            problems.append(
                "MIGRATION_DATABASE_URL must be set separately in production "
                "so the application role does not own the schema (RLS bypass)"
            )
        if any(origin == "*" for origin in self.CORS_ORIGINS):
            problems.append("CORS_ORIGINS must not contain '*' when cookies are used")

        if problems:
            raise RuntimeError("Invalid production configuration:\n  - " + "\n  - ".join(problems))


@lru_cache
def get_settings() -> Settings:
    """Cached settings instance.

    Fails loudly and readably at import time rather than producing a confusing
    stack trace on first use.
    """
    try:
        return Settings()
    except ValidationError as exc:
        missing = "\n".join(
            f"  - {'.'.join(str(p) for p in e['loc'])}: {e['msg']}" for e in exc.errors()
        )
        raise RuntimeError(
            f"Configuration error. Check your .env file (see .env.example):\n{missing}"
        ) from exc


settings = get_settings()
