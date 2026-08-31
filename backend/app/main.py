"""Application entry point.

An app factory rather than a module-level app: tests and future CLI tooling can
build an instance with different settings without import side effects.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.api.v1.health import router as health_router
from app.api.v1.router import api_router
from app.core.cache import check_redis, close_redis
from app.core.config import settings
from app.core.database import check_database, dispose_engine
from app.core.errors import register_exception_handlers
from app.core.logging import configure_logging, get_logger
from app.core.middleware import (
    AccessLogMiddleware,
    RequestContextMiddleware,
    SecurityHeadersMiddleware,
)

logger = get_logger(__name__)


@asynccontextmanager
async def lifespan(_: FastAPI) -> AsyncIterator[None]:
    configure_logging()
    settings.assert_production_ready()

    # Report dependency health at startup rather than failing to boot. A
    # database that is briefly unavailable should not prevent the process from
    # starting and then recovering -- readiness reports the real state.
    db = await check_database()
    cache = await check_redis()
    logger.info(
        "application starting",
        extra={
            "environment": settings.ENVIRONMENT,
            "database": db["status"],
            "redis": cache["status"],
        },
    )
    if db["status"] != "ok":
        logger.warning("database unreachable at startup; readiness will report not-ready")
    if cache["status"] != "ok":
        logger.warning("redis unreachable at startup; caches will fall through to postgres")

    yield

    logger.info("application shutting down")
    await dispose_engine()
    await close_redis()


def create_app() -> FastAPI:
    configure_logging()

    app = FastAPI(
        title=settings.APP_NAME,
        version="0.1.0",
        description="Operational Support Tracker API",
        openapi_url=f"{settings.API_V1_PREFIX}/openapi.json",
        docs_url="/docs" if not settings.is_production else None,
        redoc_url=None,
        lifespan=lifespan,
    )

    # Middleware runs in reverse registration order, so the last registered is
    # outermost. Request context must be outermost, so it is registered last.
    app.add_middleware(SecurityHeadersMiddleware)
    app.add_middleware(AccessLogMiddleware)
    app.add_middleware(RequestContextMiddleware)

    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.CORS_ORIGINS,
        allow_credentials=True,  # required for the auth cookies
        allow_methods=["GET", "POST", "PATCH", "PUT", "DELETE", "OPTIONS"],
        allow_headers=["Content-Type", "X-CSRF-Token", "X-Request-ID", "Idempotency-Key"],
        expose_headers=["X-Request-ID"],
    )

    register_exception_handlers(app)

    app.include_router(health_router)
    app.include_router(api_router, prefix=settings.API_V1_PREFIX)

    return app


app = create_app()
