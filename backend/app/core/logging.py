"""Structured logging.

JSON to stdout. No files, no rotation -- whatever runs the application collects
stdout. That keeps the app portable while the deployment decision is parked.

Two things matter here beyond formatting:

1. Every record carries the request id, tenant, and user from the request
   context, so one identifier finds every line for a request across every
   module it touched.

2. A redaction filter strips known-sensitive keys. This system stores HR support
   data -- ticket descriptions and comments contain salaries, personal details,
   and government identifiers, and must never reach the logs. The filter is a
   safety net for a careless call added later during debugging, not a substitute
   for care at the call site.

See docs/09-logging-and-caching.md.
"""

from __future__ import annotations

import logging
import re
import sys
from datetime import UTC, datetime
from typing import Any

from pythonjsonlogger.json import JsonFormatter as BaseJsonFormatter

from app.core.config import settings
from app.core.context import LogContext

# Keys whose values are replaced wholesale wherever they appear in log extras.
SENSITIVE_KEYS: frozenset[str] = frozenset(
    {
        # credentials and session material
        "password",
        "password_hash",
        "new_password",
        "current_password",
        "token",
        "access_token",
        "refresh_token",
        "jwt",
        "authorization",
        "cookie",
        "set_cookie",
        "csrf_token",
        "secret",
        "jwt_secret",
        "api_key",
        # HR support content -- never logged, see module docstring
        "description",
        "body",
        "comment",
        "resolution_notes",
        "steps_to_reproduce",
        "expected_result",
        "actual_result",
        "note",
        "reason",
        "original_filename",
        "reporter_email",
        "reporter_name",
        "email",
    }
)

REDACTED = "[redacted]"

_BEARER_RE = re.compile(r"(Bearer\s+)[A-Za-z0-9._\-]+", re.IGNORECASE)


def _redact(value: Any, depth: int = 0) -> Any:
    """Recursively redact sensitive keys in mappings and sequences."""
    if depth > 6:
        return value
    if isinstance(value, dict):
        return {
            key: (REDACTED if str(key).lower() in SENSITIVE_KEYS else _redact(val, depth + 1))
            for key, val in value.items()
        }
    if isinstance(value, (list, tuple)):
        return [_redact(item, depth + 1) for item in value]
    if isinstance(value, str):
        return _BEARER_RE.sub(r"\1" + REDACTED, value)
    return value


class ContextFilter(logging.Filter):
    """Attaches request context and applies redaction to every record."""

    def filter(self, record: logging.LogRecord) -> bool:
        ctx = LogContext.current()
        record.request_id = ctx.request_id
        record.tenant_id = str(ctx.tenant_id) if ctx.tenant_id else None
        record.user_id = str(ctx.user_id) if ctx.user_id else None

        for key, value in list(record.__dict__.items()):
            if key.startswith("_") or key in _RESERVED:
                continue
            if key.lower() in SENSITIVE_KEYS:
                record.__dict__[key] = REDACTED
            elif isinstance(value, (dict, list, tuple, str)):
                # Strings are included so a bearer token pasted into an
                # otherwise innocuous field is still scrubbed.
                record.__dict__[key] = _redact(value)

        # The message itself is a common accidental leak:
        #   logger.info(f"calling upstream with {auth_header}")
        if isinstance(record.msg, str):
            record.msg = _BEARER_RE.sub(r"\1" + REDACTED, record.msg)
        return True


_RESERVED = frozenset(logging.LogRecord("", 0, "", 0, "", None, None).__dict__.keys()) | {
    "request_id",
    "tenant_id",
    "user_id",
    "asctime",
    "message",
}


class JsonFormatter(BaseJsonFormatter):
    """Stable field names for machine consumption.

    Timestamps are ISO 8601 in UTC with milliseconds. Building them from
    ``datetime`` rather than ``formatTime`` matters: ``strftime`` has no
    sub-second directive, so a format string cannot produce them.
    """

    def add_fields(
        self,
        log_record: dict[str, Any],
        record: logging.LogRecord,
        message_dict: dict[str, Any],
    ) -> None:
        super().add_fields(log_record, record, message_dict)
        created = datetime.fromtimestamp(record.created, tz=UTC)
        log_record["timestamp"] = created.isoformat(timespec="milliseconds").replace("+00:00", "Z")
        log_record["level"] = record.levelname
        log_record["logger"] = record.name
        log_record.pop("taskName", None)
        # Drop empty correlation fields so lines stay readable
        for key in ("request_id", "tenant_id", "user_id"):
            if log_record.get(key) is None:
                log_record.pop(key, None)


class ConsoleFormatter(logging.Formatter):
    """Human-readable formatter for local development."""

    def format(self, record: logging.LogRecord) -> str:
        base = super().format(record)
        rid = getattr(record, "request_id", None)
        return f"{base}  [req {rid[:8]}]" if rid else base


def configure_logging() -> None:
    """Install handlers. Idempotent -- safe to call more than once."""
    root = logging.getLogger()
    root.handlers.clear()
    root.setLevel(settings.LOG_LEVEL)

    handler = logging.StreamHandler(sys.stdout)
    handler.addFilter(ContextFilter())

    if settings.LOG_JSON:
        handler.setFormatter(JsonFormatter("%(timestamp)s %(level)s %(logger)s %(message)s"))
    else:
        handler.setFormatter(
            ConsoleFormatter("%(asctime)s  %(levelname)-8s %(name)-38s  %(message)s")
        )

    root.addHandler(handler)

    # Uvicorn ships its own handlers; route them through ours so every line
    # is structured and correlated.
    for name in ("uvicorn", "uvicorn.error", "uvicorn.access"):
        logger = logging.getLogger(name)
        logger.handlers.clear()
        logger.propagate = True

    # Access logging is emitted by our own middleware, with richer fields.
    logging.getLogger("uvicorn.access").disabled = True

    logging.getLogger("sqlalchemy.engine").setLevel(
        logging.INFO if settings.DB_ECHO else logging.WARNING
    )


def get_logger(name: str) -> logging.Logger:
    return logging.getLogger(name)
