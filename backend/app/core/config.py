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
