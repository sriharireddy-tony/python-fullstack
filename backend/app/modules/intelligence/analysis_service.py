"""Running the analysis agent, and recording what it did.

Kept out of `service.py` because it is a different shape of work. Similar-issue
search answers inside a request; an analysis run takes tens of seconds and
several LLM calls, so it is queued, executed by the worker, and polled.

## Why 202-and-poll rather than a synchronous request

Three reasons, in order of how much they matter:

1. **It can take a minute.** Eight turns at several seconds each is not an HTTP
   response, and a request that long is a held worker, a proxy timeout, and a
   user staring at a spinner with no idea whether anything is happening.
2. **The step log is worth streaming.** Polling shows turns as they land, so
   the wait tells the user what the agent is doing rather than nothing.
3. **A dropped connection must not waste the work.** The run is a row; the
   browser closing does not cancel it, and the report is there when the user
   comes back.

The queue is the outbox already built in Phase A. No new infrastructure — which
is the return on having built the outbox rather than reaching for a broker.
"""

from __future__ import annotations

import time
import uuid
from datetime import UTC, datetime
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.database import commit_preserving_scope
from app.core.errors import ConflictError, NotFoundError
from app.core.logging import get_logger
from app.modules.intelligence.deps import AiDeps
from app.modules.intelligence.graphs.analysis import (
    AgentDeps,
    AnalysisState,
    run_analysis,
)
from app.modules.intelligence.guardrails.budget import Wallclock
from app.modules.intelligence.models import (
    AiAnalysisRun,
    AiAnalysisStep,
    AiJobKind,
    AnalysisStatus,
)
from app.modules.intelligence.registry import ModelRegistry
from app.modules.intelligence.repository import AiJobRepository, AnalysisRepository
from app.modules.intelligence.tools.registry import build_tools
from app.modules.intelligence.tools.tickets import ToolContext
from app.modules.tickets.models import Ticket

logger = get_logger(__name__)

#: How long a queued or running analysis blocks a new request for the same
#: ticket.
#:
#: The window is the important part, and it was missing at first. Deduping on
#: status alone looks right and is a trap: a worker killed mid-run leaves a row
#: in `running` that never reaches a terminal status, and that ticket can then
#: **never** be analysed again. Found by hitting it -- a probe crashed, and the
#: next request was refused for a run that had stopped existing.
#:
#: Comfortably longer than the agent's own wall-clock deadline, so a slow but
#: healthy run is never treated as abandoned.
STALE_RUN_SECONDS = settings.AGENT_TIMEOUT_SECONDS * 2


def _blocks_new_run(run: AiAnalysisRun) -> bool:
    """Whether an existing run should refuse a new request.

    Only an *active* run blocks. A run past the stale window is assumed
    abandoned -- its worker is gone -- and a new request is allowed rather than
    the ticket being locked out permanently.
    """
    if run.status not in (AnalysisStatus.QUEUED, AnalysisStatus.RUNNING):
        return False
    reference_time = run.started_at or run.created_at
    age = (datetime.now(UTC) - reference_time).total_seconds()
    if age > STALE_RUN_SECONDS:
        logger.warning(
            "ignoring a stale analysis run",
            extra={"run_id": str(run.id), "status": run.status.value, "age_seconds": int(age)},
        )
        return False
    return True


class AnalysisService:
    def __init__(self, session: AsyncSession, tenant_id: uuid.UUID) -> None:
        self._session = session
        self._tenant_id = tenant_id
        self._runs = AnalysisRepository(session)
        self._jobs = AiJobRepository(session)

    async def request(self, ticket: Ticket, user_id: uuid.UUID) -> AiAnalysisRun:
        """Queue an analysis and return immediately.

        The run row and the outbox job are written in **one transaction**, so a
        queued run always has a job and a job always has a run. Two writes with
        a commit between them is the dual-write problem, and its symptom here
        would be a run stuck in `queued` forever with nothing to execute it.
        """
        existing = await self._runs.latest_for_ticket(ticket.id)
        if existing is not None and _blocks_new_run(existing):
            raise ConflictError("An analysis for this ticket is already running.")

        run = await self._runs.create(self._tenant_id, ticket.id, user_id)
        await self._jobs.enqueue(
            self._tenant_id,
            AiJobKind.ANALYSE_TICKET,
            {"run_id": str(run.id), "ticket_id": str(ticket.id)},
        )
        logger.info(
            "analysis queued",
            extra={"run_id": str(run.id), "reference": ticket.reference},
        )
        return run

    async def get(self, run_id: uuid.UUID) -> tuple[AiAnalysisRun, list[AiAnalysisStep]]:
        run = await self._runs.get(run_id)
        if run is None:
            raise NotFoundError("Analysis run not found.")
        return run, await self._runs.steps(run_id)

    async def latest(
        self, ticket_id: uuid.UUID
    ) -> tuple[AiAnalysisRun, list[AiAnalysisStep]] | None:
        run = await self._runs.latest_for_ticket(ticket_id)
        if run is None:
            return None
        return run, await self._runs.steps(run.id)

    async def rate(self, run_id: uuid.UUID, helpful: bool) -> AiAnalysisRun:
        run = await self._runs.rate(run_id, helpful)
        if run is None:
            raise NotFoundError("Analysis run not found.")
        logger.info("analysis rated", extra={"run_id": str(run_id), "helpful": helpful})
        return run

    async def _commit(self) -> None:
        """Commit without losing the RLS tenant scope.

        The agent is the one place in this codebase that commits mid-flow, and
        `set_config(..., is_local => true)` is transaction-scoped -- so a plain
        `commit()` silently unsets the tenant and every later statement is
        filtered to nothing. See `commit_preserving_scope`.
        """
        await commit_preserving_scope(self._session, self._tenant_id)

    # ----------------------------------------------------------- execution

    async def execute(self, run_id: uuid.UUID, ticket: Ticket, ai: AiDeps) -> AnalysisState:
        """Run the agent. Called by the worker, never by a request handler.

        Everything about this method assumes it can fail badly: the model may
        hang, a tool may raise, a limit may fire. So the run is marked
        `running` and **committed** before the agent starts, the report is
        written in a separate transaction afterwards, and the step rows are
        committed as they happen. A crash mid-run therefore leaves a `running`
        row with a partial step log — which is diagnosable — rather than
        nothing at all.
        """
        # The run row must still exist. A job whose run has been deleted is
        # ordinary -- someone purged verification data, a retention job ran --
        # and the symptom without this check is baffling: `mark_running`
        # updates zero rows without complaint, the agent runs a full analysis,
        # and the *first step insert* fails on a foreign key. Diagnosing that
        # from the traceback means working backwards through a completed
        # agent run to a row that was never there.
        if await self._runs.get(run_id) is None:
            logger.info("analysis skipped: the run row is gone", extra={"run_id": str(run_id)})
            return {}

        await self._runs.mark_running(run_id)
        await self._commit()

        clock = Wallclock(settings.AGENT_TIMEOUT_SECONDS)
        started = time.perf_counter()

        requested_by = (await self._runs.get(run_id)) or None
        user_id = requested_by.requested_by_id if requested_by else None

        async def record_step(
            turn: int,
            tool_name: str | None,
            tool_args: dict[str, Any] | None,
            summary: str,
        ) -> None:
            """Persist one step, committing immediately.

            Committed per step rather than at the end, so the polling endpoint
            can show progress while the run is still going. That is the whole
            reason the wait is tolerable: the user sees "searched for similar
            tickets, found 4" rather than a spinner.
            """
            await self._runs.add_step(self._tenant_id, run_id, turn, tool_name, tool_args, summary)
            await self._commit()

        deps = AgentDeps(
            registry=ModelRegistry(self._session, self._tenant_id),
            tools={
                tool.name: tool
                for tool in build_tools(
                    self._session,
                    ToolContext(
                        tenant_id=self._tenant_id,
                        user_id=user_id or uuid.UUID(int=0),
                    ),
                    ai,
                )
            },
            clock=clock,
            record_step=record_step,
            max_turns=settings.AGENT_MAX_TURNS,
            max_tool_calls=settings.AGENT_MAX_TOOL_CALLS,
            max_cost=settings.AGENT_MAX_COST,
        )

        state: AnalysisState = {
            "tenant_id": self._tenant_id,
            "run_id": run_id,
            "ticket_id": ticket.id,
            "ticket_reference": ticket.reference,
        }

        try:
            final = await run_analysis(state, deps)
        except Exception as exc:
            # The graph itself failed, which is a bug rather than a limit.
            # Recorded as failed with the exception type, in a fresh
            # transaction because the failing one may be unusable.
            logger.exception("analysis run crashed", extra={"run_id": str(run_id)})
            await self._session.rollback()
            await self._runs.finish(
                run_id,
                status=AnalysisStatus.FAILED,
                report=None,
                partial=True,
                model=None,
                used_fallback=False,
                turns=0,
                tool_calls=0,
                estimated_cost=0.0,
                latency_ms=int((time.perf_counter() - started) * 1000),
                error=f"{type(exc).__name__}: {exc}"[:2000],
            )
            await self._commit()
            raise

        report = final.get("report")
        partial = bool(final.get("partial"))
        status = _status_for(final, partial=partial, report_written=report is not None)

        await self._runs.finish(
            run_id,
            status=status,
            report=_report_payload(final),
            partial=partial,
            model=final.get("model"),
            # A weaker model produced this, so the report says so. A reader
            # calibrating on "the AI said" deserves to know which AI.
            used_fallback=final.get("model", "") == settings.OLLAMA_CHAT_MODEL,
            turns=final.get("turn", 0),
            tool_calls=final.get("tool_calls", 0),
            estimated_cost=final.get("estimated_cost", 0.0),
            latency_ms=int((time.perf_counter() - started) * 1000),
            error=final.get("stop_reason") if partial else None,
        )
        await self._commit()

        logger.info(
            "analysis complete",
            extra={
                "run_id": str(run_id),
                "status": status.value,
                "turns": final.get("turn", 0),
                "tool_calls": final.get("tool_calls", 0),
                "partial": partial,
                "seconds": round(time.perf_counter() - started, 1),
            },
        )
        return final


def _status_for(final: AnalysisState, *, partial: bool, report_written: bool) -> AnalysisStatus:
    """Which terminal status a finished run gets.

    Three outcomes rather than two, because "stopped at a limit but produced a
    report" is genuinely different from both success and failure, and squashing
    it into either loses the information a reader needs.
    """
    if not report_written:
        return AnalysisStatus.FAILED
    if not partial:
        return AnalysisStatus.COMPLETED
    reason = final.get("stop_reason", "")
    if "time" in reason:
        return AnalysisStatus.TIMED_OUT
    if "cost" in reason or "limit" in reason:
        return AnalysisStatus.BUDGET_EXCEEDED
    return AnalysisStatus.COMPLETED


def _report_payload(final: AnalysisState) -> dict[str, Any] | None:
    """Serialise the report, plus the trace that produced it.

    The trace is stored with the report rather than only logged, because six
    months later the log line is gone and the question "did this run actually
    search for duplicates" is still worth answering.
    """
    report = final.get("report")
    if report is None:
        return None
    payload = report.model_dump()
    payload["_trace"] = final.get("trace", [])
    payload["_stop_reason"] = final.get("stop_reason", "")
    return payload
