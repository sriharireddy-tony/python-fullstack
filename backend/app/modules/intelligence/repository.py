"""Data access for the AI layer.

Queries only. The job-claiming query is the interesting one — see `claim_jobs`.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from typing import Any, cast

from sqlalchemy import CursorResult, delete, func, select, text, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.modules.intelligence.models import (
    AiAnalysisRun,
    AiAnalysisStep,
    AiJob,
    AiJobKind,
    AiJobStatus,
    AiSuggestion,
    AiUsage,
    AnalysisStatus,
    SuggestionKind,
    SuggestionStatus,
    TicketVectorState,
)

#: A job stuck in `running` past this is assumed to belong to a crashed worker
#: and is returned to the queue. Long enough that a slow embed is not stolen
#: mid-flight, short enough that a crash is not a permanent stall.
STALE_LOCK_MINUTES = 15

#: After this many failures a job stops being retried. Without a ceiling, a
#: permanently broken job is retried forever and hides real failures in the log.
MAX_ATTEMPTS = 5


class AiJobRepository:
    """The outbox.

    Deliberately not tenant-scoped: the worker runs with no tenant context and
    reads the scope *from* the row. An RLS policy here would make every job
    unclaimable.
    """

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def enqueue(
        self,
        tenant_id: uuid.UUID,
        kind: AiJobKind,
        payload: dict[str, Any],
        delay_seconds: int = 0,
    ) -> AiJob:
        """Add a job.

        Called *inside the caller's transaction*, so the job's existence is
        atomic with whatever change triggered it. That is the whole point of an
        outbox: pushing to an external broker instead would be a dual write.
        """
        job = AiJob(
            tenant_id=tenant_id,
            kind=kind,
            payload=payload,
            status=AiJobStatus.QUEUED,
            scheduled_at=datetime.now(UTC) + timedelta(seconds=delay_seconds),
        )
        self._session.add(job)
        await self._session.flush()
        return job

    async def claim(self, worker_id: str, limit: int = 10) -> list[AiJob]:
        """Claim up to `limit` queued jobs.

        `FOR UPDATE SKIP LOCKED` is what makes this safe with several workers:
        rows another worker holds are stepped *over* rather than waited on. No
        broker required, and correct at any worker count.
        """
        result = await self._session.execute(
            text(
                """
                SELECT id FROM ai_jobs
                WHERE status = 'queued' AND scheduled_at <= now()
                ORDER BY scheduled_at, created_at
                FOR UPDATE SKIP LOCKED
                LIMIT :limit
                """
            ),
            {"limit": limit},
        )
        job_ids = [row[0] for row in result.all()]
        if not job_ids:
            return []

        await self._session.execute(
            update(AiJob)
            .where(AiJob.id.in_(job_ids))
            .values(
                status=AiJobStatus.RUNNING,
                locked_at=datetime.now(UTC),
                locked_by=worker_id,
                attempts=AiJob.attempts + 1,
            )
        )
        rows = await self._session.execute(select(AiJob).where(AiJob.id.in_(job_ids)))
        return list(rows.scalars().all())

    async def complete(self, job_id: uuid.UUID) -> None:
        await self._session.execute(
            update(AiJob).where(AiJob.id == job_id).values(status=AiJobStatus.DONE)
        )

    async def fail(self, job_id: uuid.UUID, error: str) -> None:
        """Return a job to the queue, or give up after MAX_ATTEMPTS."""
        job = await self._session.get(AiJob, job_id)
        if job is None:
            return
        if job.attempts >= MAX_ATTEMPTS:
            job.status = AiJobStatus.FAILED
        else:
            job.status = AiJobStatus.QUEUED
            # Exponential backoff, so a transient outage is not hammered.
            job.scheduled_at = datetime.now(UTC) + timedelta(seconds=30 * 2**job.attempts)
        job.last_error = error[:2000]
        job.locked_at = None
        job.locked_by = None

    async def release_stale(self) -> int:
        """Return jobs from crashed workers to the queue."""
        cutoff = datetime.now(UTC) - timedelta(minutes=STALE_LOCK_MINUTES)
        result = cast(
            "CursorResult[Any]",
            await self._session.execute(
                text(
                    """
                    UPDATE ai_jobs
                    SET status = 'queued', locked_at = NULL, locked_by = NULL
                    WHERE status = 'running' AND locked_at < :cutoff
                    """
                ),
                {"cutoff": cutoff},
            ),
        )
        return result.rowcount or 0

    async def stats(self) -> dict[str, int]:
        rows = await self._session.execute(
            select(AiJob.status, func.count()).group_by(AiJob.status)
        )
        return {status.value: count for status, count in rows.all()}

    async def purge_done(self, older_than_days: int = 7) -> int:
        cutoff = datetime.now(UTC) - timedelta(days=older_than_days)
        result = cast(
            "CursorResult[Any]",
            await self._session.execute(
                delete(AiJob).where(AiJob.status == AiJobStatus.DONE, AiJob.created_at < cutoff)
            ),
        )
        return result.rowcount or 0


class VectorStateRepository:
    """Bookkeeping for what has been embedded.

    This table is what makes reconciliation possible without reading every id
    out of Pinecone and diffing.
    """

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def get(self, ticket_id: uuid.UUID, model: str) -> TicketVectorState | None:
        result = await self._session.execute(
            select(TicketVectorState).where(
                TicketVectorState.ticket_id == ticket_id,
                TicketVectorState.model == model,
            )
        )
        return result.scalar_one_or_none()

    async def record(
        self,
        tenant_id: uuid.UUID,
        ticket_id: uuid.UUID,
        model: str,
        dimension: int,
        source_hash: str,
    ) -> None:
        existing = await self.get(ticket_id, model)
        if existing is None:
            self._session.add(
                TicketVectorState(
                    tenant_id=tenant_id,
                    ticket_id=ticket_id,
                    model=model,
                    dimension=dimension,
                    source_hash=source_hash,
                    synced_at=datetime.now(UTC),
                )
            )
        else:
            existing.source_hash = source_hash
            existing.dimension = dimension
            existing.synced_at = datetime.now(UTC)
        await self._session.flush()

    async def forget(self, ticket_ids: list[uuid.UUID]) -> None:
        if not ticket_ids:
            return
        await self._session.execute(
            delete(TicketVectorState).where(TicketVectorState.ticket_id.in_(ticket_ids))
        )

    async def embedded_ids(self, model: str) -> set[uuid.UUID]:
        rows = await self._session.execute(
            select(TicketVectorState.ticket_id).where(TicketVectorState.model == model)
        )
        return set(rows.scalars().all())

    async def count(self, model: str) -> int:
        return (
            await self._session.scalar(
                select(func.count())
                .select_from(TicketVectorState)
                .where(TicketVectorState.model == model)
            )
            or 0
        )


class UsageRepository:
    """Per-call cost attribution.

    Turns "the AI features cost too much" into a query, and is the basis for
    cost per *accepted* suggestion — the number worth optimising.
    """

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def record(
        self,
        *,
        tenant_id: uuid.UUID | None,
        feature: str,
        role: str,
        model: str,
        input_tokens: int = 0,
        output_tokens: int = 0,
        cached_tokens: int = 0,
        estimated_cost: float = 0.0,
        latency_ms: int | None = None,
        succeeded: bool = True,
    ) -> None:
        self._session.add(
            AiUsage(
                tenant_id=tenant_id,
                feature=feature,
                role=role,
                model=model,
                input_tokens=input_tokens,
                output_tokens=output_tokens,
                cached_tokens=cached_tokens,
                estimated_cost=estimated_cost,
                latency_ms=latency_ms,
                succeeded=succeeded,
            )
        )
        await self._session.flush()

    async def calls_today(self, model: str, role: str | None = None) -> int:
        """Our own daily counter, per model and optionally per role.

        The provider's quota counter is not visible to us, so a pre-flight
        check against this is how exhaustion is anticipated rather than
        discovered through a 429 mid-run.

        The `role` filter matters more than it looks. Two roles can share a
        model -- reranking and query rewriting both use the cheap hosted one --
        and counting by model alone means their budgets are the same counter.
        The observed effect: a day of reranking (limit 250) silently exhausted
        the rewrite budget (limit 100), and the rewrite role degraded to the
        local model for a reason its own configuration did not explain. A
        per-role budget has to be counted per role.
        """
        since = datetime.now(UTC).replace(hour=0, minute=0, second=0, microsecond=0)
        conditions = [AiUsage.model == model, AiUsage.created_at >= since]
        if role is not None:
            conditions.append(AiUsage.role == role)
        return (
            await self._session.scalar(select(func.count()).select_from(AiUsage).where(*conditions))
            or 0
        )

    async def month_cost(self, tenant_id: uuid.UUID) -> float:
        since = datetime.now(UTC).replace(day=1, hour=0, minute=0, second=0, microsecond=0)
        return float(
            await self._session.scalar(
                select(func.coalesce(func.sum(AiUsage.estimated_cost), 0.0)).where(
                    AiUsage.tenant_id == tenant_id, AiUsage.created_at >= since
                )
            )
            or 0.0
        )


class SuggestionRepository:
    """Suggestions, kept strictly separate from the ticket's own fields.

    Nothing the model produces ever lands on `tickets.duplicate_of_id`. A
    suggested duplicate is a row here with `status = pending` until a human
    accepts it, and that separation is what makes the feature safe to be wrong.

    It is also the accuracy signal: every accept and reject is a labelled pair,
    so the feature and its own evaluation dataset are the same thing. That is
    why `decided_by_id` and `decided_at` are recorded rather than just a status
    flag -- "who agreed, and when" is what makes the label trustworthy later.
    """

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def replace_similar(
        self,
        tenant_id: uuid.UUID,
        ticket_id: uuid.UUID,
        model: str | None,
        items: list[dict[str, Any]],
    ) -> list[AiSuggestion]:
        """Store this run's suggestions, superseding the previous run's.

        Pending rows are deleted; **decided rows are kept**. A human's accept or
        reject is data we paid for with someone's attention, and a re-run of the
        pipeline is not a reason to discard it. It is also the only way the
        accuracy history survives a model change.
        """
        await self._session.execute(
            delete(AiSuggestion).where(
                AiSuggestion.ticket_id == ticket_id,
                AiSuggestion.kind == SuggestionKind.SIMILAR,
                AiSuggestion.status == SuggestionStatus.PENDING,
            )
        )

        decided = await self._session.execute(
            select(AiSuggestion.related_ticket_id).where(
                AiSuggestion.ticket_id == ticket_id,
                AiSuggestion.kind == SuggestionKind.SIMILAR,
                AiSuggestion.status != SuggestionStatus.PENDING,
            )
        )
        already_judged = {row for (row,) in decided.all()}

        created: list[AiSuggestion] = []
        for item in items:
            related_id = item["related_ticket_id"]
            if related_id in already_judged:
                # Re-suggesting a pair a human already rejected is the fastest
                # way to teach people to ignore the panel.
                continue
            suggestion = AiSuggestion(
                tenant_id=tenant_id,
                ticket_id=ticket_id,
                kind=SuggestionKind.SIMILAR,
                related_ticket_id=related_id,
                relation=item.get("relation"),
                confidence=item.get("confidence"),
                reason=item.get("reason"),
                payload=item.get("payload", {}),
                model=model,
                status=SuggestionStatus.PENDING,
            )
            self._session.add(suggestion)
            created.append(suggestion)

        await self._session.flush()
        return created

    async def for_ticket(
        self, ticket_id: uuid.UUID, kind: SuggestionKind = SuggestionKind.SIMILAR
    ) -> list[AiSuggestion]:
        rows = await self._session.execute(
            select(AiSuggestion)
            .where(AiSuggestion.ticket_id == ticket_id, AiSuggestion.kind == kind)
            .order_by(AiSuggestion.confidence.desc().nullslast(), AiSuggestion.created_at)
        )
        return list(rows.scalars().all())

    async def get(self, suggestion_id: uuid.UUID) -> AiSuggestion | None:
        return await self._session.get(AiSuggestion, suggestion_id)

    async def decide(
        self,
        suggestion: AiSuggestion,
        *,
        accepted: bool,
        user_id: uuid.UUID,
    ) -> AiSuggestion:
        """Record a human's verdict.

        Idempotent on purpose: a double-click must not turn an accept into a
        conflict the user has to understand. Re-deciding the same way is a
        no-op; changing the verdict is allowed, because people do change their
        minds and the timestamp records when.
        """
        suggestion.status = SuggestionStatus.ACCEPTED if accepted else SuggestionStatus.REJECTED
        suggestion.decided_by_id = user_id
        suggestion.decided_at = datetime.now(UTC)
        await self._session.flush()
        return suggestion

    async def acceptance_rate(self, tenant_id: uuid.UUID, days: int = 30) -> dict[str, int]:
        """Accepts, rejects, and pending over a window.

        The number that actually matters. Retrieval metrics say whether the
        right ticket was found; this says whether anyone found that useful --
        and those two have been known to move in opposite directions.
        """
        since = datetime.now(UTC) - timedelta(days=days)
        rows = await self._session.execute(
            select(AiSuggestion.status, func.count())
            .where(AiSuggestion.tenant_id == tenant_id, AiSuggestion.created_at >= since)
            .group_by(AiSuggestion.status)
        )
        return {status.value: count for status, count in rows.all()}


class AnalysisRepository:
    """Agent runs and their step logs.

    The step log is the part that earns its storage. Without it, "why did the
    analysis miss the obvious duplicate?" is unanswerable -- with it you can see
    that the agent searched once with a poor query and never retried, or called
    the same tool five times with near-identical arguments. A model's output is
    not debuggable; its trajectory is.
    """

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def create(
        self,
        tenant_id: uuid.UUID,
        ticket_id: uuid.UUID,
        requested_by_id: uuid.UUID | None,
    ) -> AiAnalysisRun:
        run = AiAnalysisRun(
            tenant_id=tenant_id,
            ticket_id=ticket_id,
            requested_by_id=requested_by_id,
            status=AnalysisStatus.QUEUED,
        )
        self._session.add(run)
        await self._session.flush()
        return run

    async def get(self, run_id: uuid.UUID) -> AiAnalysisRun | None:
        return await self._session.get(AiAnalysisRun, run_id)

    async def latest_for_ticket(self, ticket_id: uuid.UUID) -> AiAnalysisRun | None:
        rows = await self._session.execute(
            select(AiAnalysisRun)
            .where(AiAnalysisRun.ticket_id == ticket_id)
            .order_by(AiAnalysisRun.created_at.desc())
            .limit(1)
        )
        return rows.scalar_one_or_none()

    async def mark_running(self, run_id: uuid.UUID) -> None:
        await self._session.execute(
            update(AiAnalysisRun)
            .where(AiAnalysisRun.id == run_id)
            .values(status=AnalysisStatus.RUNNING, started_at=datetime.now(UTC))
        )

    async def finish(
        self,
        run_id: uuid.UUID,
        *,
        status: AnalysisStatus,
        report: dict[str, Any] | None,
        partial: bool,
        model: str | None,
        used_fallback: bool,
        turns: int,
        tool_calls: int,
        estimated_cost: float,
        latency_ms: int,
        error: str | None = None,
    ) -> None:
        await self._session.execute(
            update(AiAnalysisRun)
            .where(AiAnalysisRun.id == run_id)
            .values(
                status=status,
                report=report,
                partial=partial,
                model=model,
                used_fallback_model=used_fallback,
                turns=turns,
                tool_calls=tool_calls,
                estimated_cost=estimated_cost,
                latency_ms=latency_ms,
                error=error,
                finished_at=datetime.now(UTC),
            )
        )

    async def add_step(
        self,
        tenant_id: uuid.UUID,
        run_id: uuid.UUID,
        turn: int,
        tool_name: str | None,
        tool_args: dict[str, Any] | None,
        result_summary: str,
    ) -> None:
        self._session.add(
            AiAnalysisStep(
                tenant_id=tenant_id,
                run_id=run_id,
                turn=turn,
                tool_name=tool_name,
                tool_args=tool_args,
                result_summary=result_summary[:2000],
            )
        )
        await self._session.flush()

    async def steps(self, run_id: uuid.UUID) -> list[AiAnalysisStep]:
        rows = await self._session.execute(
            select(AiAnalysisStep)
            .where(AiAnalysisStep.run_id == run_id)
            .order_by(AiAnalysisStep.turn, AiAnalysisStep.created_at)
        )
        return list(rows.scalars().all())

    async def rate(self, run_id: uuid.UUID, helpful: bool) -> AiAnalysisRun | None:
        """Record thumbs up or down.

        The only signal that says whether analyses are worth their cost. Every
        other number about the agent -- turns, tokens, latency -- describes what
        it did, not whether it helped.
        """
        run = await self._session.get(AiAnalysisRun, run_id)
        if run is None:
            return None
        run.helpful = helpful
        await self._session.flush()
        return run
