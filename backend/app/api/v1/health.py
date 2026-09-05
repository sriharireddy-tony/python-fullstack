"""Health and readiness probes.

Two endpoints, deliberately different:

* ``/health/live``  -- is the process alive? No dependencies checked. A restart
  is the only sensible response to a failure here.
* ``/health/ready`` -- can it serve traffic? Checks Postgres and Redis. A
  failure means "stop sending requests", not "restart me".

Conflating the two causes restart loops: a database blip restarts every healthy
application instance, which makes the outage worse.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Response, status

from app.core.cache import check_redis
from app.core.config import settings
from app.core.database import check_database

router = APIRouter(tags=["health"])


@router.get("/health/live", summary="Liveness probe")
async def live() -> dict[str, str]:
    return {"status": "ok", "app": settings.APP_NAME, "environment": settings.ENVIRONMENT}


@router.get("/health/ready", summary="Readiness probe")
async def ready(response: Response) -> dict[str, Any]:
    checks: dict[str, Any] = {
        "database": await check_database(),
        "redis": await check_redis(),
    }

    # Reported but never fatal: no AI call sits on a write path, so Ollama or
    # Chroma being down degrades an AI feature rather than the service.
    if settings.AI_ENABLED:
        from app.modules.intelligence.deps import ai_health

        checks.update(await ai_health())

    # Redis degradation is not fatal: caches fail open and fall through to
    # Postgres. Without a database there is nothing to serve.
    database_ok = checks["database"]["status"] == "ok"
    redis_ok = checks["redis"]["status"] == "ok"

    if not database_ok:
        overall = "error"
        response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE
    elif not redis_ok:
        overall = "degraded"
    else:
        overall = "ok"

    return {"status": overall, "checks": checks}
