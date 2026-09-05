"""The outbox worker.

Drains `ai_jobs`. Runs as its own process:

    .venv\\Scripts\\python -m app.modules.intelligence.jobs.worker

Stateless, so several can run at once — `FOR UPDATE SKIP LOCKED` handles the
contention. Each job runs in its own transaction, scoped to the tenant read
*from the job row*, so a failure rolls back only that job.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import signal
import socket
import uuid
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import session_scope
from app.core.logging import configure_logging, get_logger
from app.modules.intelligence.analysis_service import AnalysisService
from app.modules.intelligence.chat_service import ChatService
from app.modules.intelligence.deps import AiDeps, get_ai_deps
from app.modules.intelligence.embeddings.service import EmbeddingService
from app.modules.intelligence.models import AiJobKind
from app.modules.intelligence.repository import AiJobRepository
from app.modules.tenancy.models import Tenant
from app.modules.tickets.models import Ticket

logger = get_logger(__name__)

POLL_SECONDS = 3.0

#: How often housekeeping runs, counted in worker ticks.
#:
#: Roughly hourly when idle (1200 ticks at a 3-second idle backoff), and more
#: often under load, which is harmless -- both jobs it runs are cheap and
#: idempotent. Counted rather than timed because retention measured in days
#: does not need a clock, and a counter cannot drift, double-fire, or need a
#: "last run" row.
MAINTENANCE_EVERY_CYCLES = 1200
BATCH = 10
STALE_SWEEP_EVERY = 20  # poll cycles


class Worker:
    def __init__(self) -> None:
        self._id = f"{socket.gethostname()}:{os.getpid()}"
        self._stopping = asyncio.Event()
        self._cycles = 0

    def request_stop(self) -> None:
        logger.info("worker stop requested", extra={"worker": self._id})
        self._stopping.set()

    async def run(self) -> None:
        deps = await get_ai_deps()
        logger.info("worker started", extra={"worker": self._id})

        while not self._stopping.is_set():
            try:
                processed = await self._tick(deps)
            except Exception:
                logger.exception("worker tick failed")
                processed = 0

            try:
                await self._maintenance()
            except Exception:
                # Housekeeping must never take the worker down with it. A
                # failed retention pass is a warning; a stopped worker means
                # nothing gets embedded.
                logger.exception("worker maintenance failed")

            if processed == 0:
                # Idle backoff. Waiting on the stop event rather than sleeping
                # means shutdown is immediate instead of up to POLL_SECONDS.
                with contextlib.suppress(TimeoutError):
                    await asyncio.wait_for(self._stopping.wait(), timeout=POLL_SECONDS)

        logger.info("worker stopped", extra={"worker": self._id})

    async def _maintenance(self) -> None:
        """Periodic housekeeping, run on a slow cadence.

        Lives in the worker rather than in cron because the worker is already
        the process that runs continuously, and a second scheduler is a second
        thing to deploy, monitor, and forget to start. The cadence is counted
        in ticks rather than timed, which is imprecise on purpose: retention
        measured in days does not need a clock, and a counter cannot drift or
        fire twice.
        """
        if self._cycles % MAINTENANCE_EVERY_CYCLES != 0:
            return

        async with session_scope(tenant_id=None) as db:
            purged = await AiJobRepository(db).purge_done()

        # Conversation retention. Per tenant, because `expire_due` runs through
        # a tenant-scoped session -- the checkpoint rows it deletes belong to
        # threads that RLS has to have proven ownership of first.
        expired = 0
        async with session_scope(tenant_id=None) as db:
            rows = await db.execute(select(Tenant.id))
            tenant_ids = list(rows.scalars().all())

        for tenant_id in tenant_ids:
            async with session_scope(tenant_id=tenant_id) as db:
                # A system-level sweep, so there is no acting user. The service
                # requires one for ownership checks, and `expire_due` makes
                # none -- it deletes on the retention date, not on request.
                expired += await ChatService(db, tenant_id, uuid.UUID(int=0)).expire_due()

        if purged or expired:
            logger.info(
                "maintenance complete",
                extra={"purged_jobs": purged, "expired_conversations": expired},
            )

    async def _tick(self, deps: AiDeps) -> int:
        self._cycles += 1

        if self._cycles % STALE_SWEEP_EVERY == 0:
            async with session_scope(tenant_id=None) as db:
                released = await AiJobRepository(db).release_stale()
            if released:
                logger.warning("released stale jobs", extra={"count": released})

        # Claim in its own transaction and commit immediately, so the claim is
        # durable before any slow work begins. Holding the claim open across an
        # embed call would block other workers for the duration.
        async with session_scope(tenant_id=None) as db:
            jobs = await AiJobRepository(db).claim(self._id, limit=BATCH)
            claimed = [(j.id, j.tenant_id, j.kind, dict(j.payload)) for j in jobs]

        for job_id, tenant_id, kind, payload in claimed:
            await self._run_job(deps, job_id, tenant_id, kind, payload)

        return len(claimed)

    async def _run_job(
        self,
        deps: AiDeps,
        job_id: uuid.UUID,
        tenant_id: uuid.UUID,
        kind: AiJobKind,
        payload: dict[str, object],
    ) -> None:
        try:
            # Scoped to the tenant from the job row. Every tenant-scoped table
            # the job touches is then covered by RLS as usual.
            async with session_scope(tenant_id=tenant_id) as db:
                service = EmbeddingService(db, deps.embeddings, deps.store)

                if kind is AiJobKind.EMBED_TICKET:
                    ticket_id = uuid.UUID(str(payload["ticket_id"]))
                    await service.embed_ticket_id(ticket_id)

                elif kind is AiJobKind.DELETE_VECTOR:
                    ticket_id = uuid.UUID(str(payload["ticket_id"]))
                    await service.remove_ticket(tenant_id, ticket_id)

                elif kind is AiJobKind.ANALYSE_TICKET:
                    # The agent run. Minutes rather than milliseconds, several
                    # LLM calls, and it commits as it goes -- which is exactly
                    # why it belongs in the worker and not in a request.
                    await _run_analysis(db, tenant_id, payload, deps)

                else:
                    raise ValueError(f"unknown job kind: {kind}")

            async with session_scope(tenant_id=None) as db:
                await AiJobRepository(db).complete(job_id)

        except Exception as exc:
            logger.exception("job failed", extra={"job_kind": kind.value})
            # Recorded in a fresh transaction: the job's own transaction rolled
            # back, so the failure has to be written separately or the retry
            # count never increases and the job loops forever.
            async with session_scope(tenant_id=None) as db:
                await AiJobRepository(db).fail(job_id, f"{type(exc).__name__}: {exc}")


async def main() -> None:
    configure_logging()
    worker = Worker()

    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        with contextlib.suppress(NotImplementedError, AttributeError):
            loop.add_signal_handler(sig, worker.request_stop)

    await worker.run()


if __name__ == "__main__":
    with contextlib.suppress(KeyboardInterrupt):
        asyncio.run(main())


async def _run_analysis(
    db: AsyncSession,
    tenant_id: uuid.UUID,
    payload: dict[str, Any],
    deps: AiDeps,
) -> None:
    """Execute one queued analysis.

    Loads the ticket first and returns quietly if it is gone: a ticket deleted
    between the request and the run is ordinary, and failing the job would
    retry it five times against a ticket that will never exist.
    """
    run_id = uuid.UUID(str(payload["run_id"]))
    ticket_id = uuid.UUID(str(payload["ticket_id"]))

    result = await db.execute(
        select(Ticket).where(Ticket.id == ticket_id, Ticket.deleted_at.is_(None))
    )
    ticket = result.scalar_one_or_none()
    if ticket is None:
        logger.info(
            "analysis skipped: ticket is gone",
            extra={"run_id": str(run_id), "ticket_id": str(ticket_id)},
        )
        return

    await AnalysisService(db, tenant_id).execute(run_id, ticket, deps)
