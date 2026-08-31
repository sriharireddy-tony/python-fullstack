"""HTTP middleware: request correlation, access logging, security headers.

Order matters. Request-id binding runs first so that every later line -- access
log, error handler, anything a service logs -- carries the correlation id.
"""

from __future__ import annotations

import time
import uuid

from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint
from starlette.requests import Request
from starlette.responses import Response
from starlette.types import ASGIApp

from app.core.config import settings
from app.core.context import set_request_id
from app.core.logging import get_logger

logger = get_logger("app.access")

REQUEST_ID_HEADER = "X-Request-ID"

# Probes are noisy and carry no information when healthy.
_QUIET_PATHS = frozenset({"/health/live", "/health/ready"})


class RequestContextMiddleware(BaseHTTPMiddleware):
    """Binds a request id for the lifetime of the request.

    An inbound X-Request-ID is honoured so a correlation id can be traced across
    a proxy or another service; otherwise one is generated.
    """

    async def dispatch(self, request: Request, call_next: RequestResponseEndpoint) -> Response:
        incoming = request.headers.get(REQUEST_ID_HEADER)
        request_id = incoming if incoming and len(incoming) <= 128 else uuid.uuid4().hex
        token = set_request_id(request_id)
        try:
            response = await call_next(request)
        finally:
            # Reset so the value cannot leak into an unrelated task on this loop.
            from app.core.context import _request_id

            _request_id.reset(token)
        response.headers[REQUEST_ID_HEADER] = request_id
        return response


class AccessLogMiddleware(BaseHTTPMiddleware):
    """One structured line per request.

    Replaces uvicorn's access log, which cannot see the tenant, user, or
    request id.
    """

    async def dispatch(self, request: Request, call_next: RequestResponseEndpoint) -> Response:
        started = time.perf_counter()
        try:
            response = await call_next(request)
        except Exception:
            # The exception handler produces the response; this records timing
            # and re-raises so nothing is swallowed.
            duration_ms = round((time.perf_counter() - started) * 1000, 2)
            logger.exception(
                "request errored",
                extra={
                    "method": request.method,
                    "path": request.url.path,
                    "status": 500,
                    "duration_ms": duration_ms,
                },
            )
            raise

        duration_ms = round((time.perf_counter() - started) * 1000, 2)
        if request.url.path not in _QUIET_PATHS:
            logger.info(
                "request completed",
                extra={
                    "method": request.method,
                    "path": request.url.path,
                    "status": response.status_code,
                    "duration_ms": duration_ms,
                },
            )
        response.headers["X-Response-Time-ms"] = str(duration_ms)
        return response


class SecurityHeadersMiddleware(BaseHTTPMiddleware):
    """Baseline security headers on every response.

    See docs/08-security.md. HSTS is only sent over HTTPS, so it is omitted
    locally where it would pin localhost to https in the browser.
    """

    def __init__(self, app: ASGIApp) -> None:
        super().__init__(app)
        self._headers = {
            "X-Content-Type-Options": "nosniff",
            "X-Frame-Options": "DENY",
            "Referrer-Policy": "strict-origin-when-cross-origin",
            "Cross-Origin-Opener-Policy": "same-origin",
        }
        if settings.is_production:
            self._headers["Strict-Transport-Security"] = "max-age=31536000; includeSubDomains"

    async def dispatch(self, request: Request, call_next: RequestResponseEndpoint) -> Response:
        response = await call_next(request)
        for key, value in self._headers.items():
            response.headers.setdefault(key, value)
        return response
