"""Exception hierarchy and the RFC 9457 Problem Details response shape.

Services raise domain exceptions. A single handler turns them into HTTP
responses. Route handlers never construct HTTPException themselves -- that keeps
the service layer free of HTTP concerns, so background jobs and future
automation get identical behaviour.

Two rules encoded here:

* Every error carries a stable machine-readable ``code``. The frontend switches
  on the code, never on the human message, so wording can change freely.
* Cross-tenant access returns 404, never 403. The API must not confirm that a
  resource exists in another tenant.

See docs/06-api.md.
"""

from __future__ import annotations

from typing import Any

from fastapi import FastAPI, Request, status
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from app.core.context import get_request_id
from app.core.logging import get_logger

logger = get_logger(__name__)

PROBLEM_BASE_URI = "https://os-tracker/errors"
CONTENT_TYPE = "application/problem+json"


class AppError(Exception):
    """Base class for every expected application error."""

    status_code: int = status.HTTP_500_INTERNAL_SERVER_ERROR
    code: str = "INTERNAL_ERROR"
    title: str = "Internal server error"

    def __init__(
        self,
        detail: str | None = None,
        *,
        code: str | None = None,
        errors: list[dict[str, Any]] | None = None,
    ) -> None:
        self.detail = detail or self.title
        if code:
            self.code = code
        self.errors = errors or []
        super().__init__(self.detail)


class NotFoundError(AppError):
    status_code = status.HTTP_404_NOT_FOUND
    code = "RESOURCE_NOT_FOUND"
    title = "Resource not found"


class PermissionDeniedError(AppError):
    status_code = status.HTTP_403_FORBIDDEN
    code = "PERMISSION_DENIED"
    title = "Permission denied"


class AuthenticationError(AppError):
    status_code = status.HTTP_401_UNAUTHORIZED
    code = "NOT_AUTHENTICATED"
    title = "Not authenticated"


class ValidationFailedError(AppError):
    status_code = status.HTTP_422_UNPROCESSABLE_CONTENT
    code = "VALIDATION_FAILED"
    title = "Validation failed"


class ConflictError(AppError):
    status_code = status.HTTP_409_CONFLICT
    code = "CONFLICT"
    title = "Conflict"


class VersionConflictError(ConflictError):
    code = "TICKET_VERSION_CONFLICT"
    title = "Resource was modified"

    def __init__(self, detail: str | None = None) -> None:
        super().__init__(
            detail or "This record was updated by another user. Reload it and try again.",
        )


class InvalidTransitionError(ConflictError):
    code = "INVALID_TRANSITION"
    title = "Invalid state transition"


class RateLimitError(AppError):
    status_code = status.HTTP_429_TOO_MANY_REQUESTS
    code = "RATE_LIMITED"
    title = "Too many requests"


class ServiceUnavailableError(AppError):
    status_code = status.HTTP_503_SERVICE_UNAVAILABLE
    code = "SERVICE_UNAVAILABLE"
    title = "Service unavailable"


def problem_response(
    *,
    status_code: int,
    code: str,
    title: str,
    detail: str,
    errors: list[dict[str, Any]] | None = None,
) -> JSONResponse:
    body: dict[str, Any] = {
        "type": f"{PROBLEM_BASE_URI}/{code.lower().replace('_', '-')}",
        "title": title,
        "status": status_code,
        "detail": detail,
        "code": code,
        "request_id": get_request_id(),
    }
    if errors:
        body["errors"] = errors
    return JSONResponse(status_code=status_code, content=body, media_type=CONTENT_TYPE)


def register_exception_handlers(app: FastAPI) -> None:
    @app.exception_handler(AppError)
    async def _app_error(_: Request, exc: AppError) -> JSONResponse:
        # Expected errors are operational signal, not failures. 4xx at WARNING
        # keeps ERROR meaningful for things that actually need attention.
        logger.warning(
            "request failed",
            extra={"error_code": exc.code, "status": exc.status_code},
        )
        return problem_response(
            status_code=exc.status_code,
            code=exc.code,
            title=exc.title,
            detail=exc.detail,
            errors=exc.errors,
        )

    @app.exception_handler(RequestValidationError)
    async def _validation_error(_: Request, exc: RequestValidationError) -> JSONResponse:
        errors = [
            {
                "field": ".".join(str(part) for part in err["loc"][1:]) or str(err["loc"][0]),
                "message": err["msg"],
            }
            for err in exc.errors()
        ]
        return problem_response(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            code="VALIDATION_FAILED",
            title="Validation failed",
            detail="One or more fields are invalid.",
            errors=errors,
        )

    @app.exception_handler(StarletteHTTPException)
    async def _http_error(_: Request, exc: StarletteHTTPException) -> JSONResponse:
        code = {
            401: "NOT_AUTHENTICATED",
            403: "PERMISSION_DENIED",
            404: "RESOURCE_NOT_FOUND",
            405: "METHOD_NOT_ALLOWED",
        }.get(exc.status_code, "HTTP_ERROR")
        return problem_response(
            status_code=exc.status_code,
            code=code,
            title=str(exc.detail),
            detail=str(exc.detail),
        )

    @app.exception_handler(Exception)
    async def _unhandled(_: Request, exc: Exception) -> JSONResponse:
        # Full detail to the logs, a request id to the client. Internal
        # information never crosses the boundary.
        logger.exception("unhandled exception", extra={"error_type": type(exc).__name__})
        return problem_response(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            code="INTERNAL_ERROR",
            title="Internal server error",
            detail="An unexpected error occurred. Quote the request id when reporting this.",
        )
