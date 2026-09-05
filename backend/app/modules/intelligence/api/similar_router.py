"""AI endpoints.

Mounted under ``/tickets`` even though it lives in the ``intelligence``
module. The alternative — adding the route to the tickets router — would make
``tickets`` import ``intelligence``, reversing the one dependency direction
this module's design rests on. The URL belongs to the ticket; the code belongs
to the AI layer, and those two facts do not have to agree.
"""

from __future__ import annotations

import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, Query

from app.core.cache import Cache
from app.core.config import settings
from app.core.errors import RateLimitError, ServiceUnavailableError
from app.core.logging import get_logger
from app.core.permissions import Permission
from app.modules.identity.dependencies import RequestContext, ScopedDb, require
from app.modules.intelligence.analysis_service import AnalysisService
from app.modules.intelligence.deps import get_ai_deps
from app.modules.intelligence.models import AiAnalysisRun, AiAnalysisStep
from app.modules.intelligence.schemas.api import (
    AnalysisRating,
    AnalysisReportOut,
    AnalysisRunOut,
    AnalysisStepOut,
    SimilarTicketOut,
    SimilarTicketsResponse,
    SuggestionDecision,
    SuggestionOut,
)
from app.modules.intelligence.security.budget import check_user_rate
from app.modules.intelligence.service import IntelligenceService, similarity_enabled
from app.modules.tickets.models import Ticket
from app.modules.tickets.service import TicketService

logger = get_logger(__name__)

router = APIRouter()

#: Reading similar tickets needs exactly the permission that reading tickets
#: needs. An AI feature that surfaces rows the caller could not otherwise fetch
#: would be an authorization bypass with a friendly name.
CanRead = Annotated[RequestContext, Depends(require(Permission.TICKET_READ))]

#: Deciding a suggestion needs the permission to *update* a ticket, not merely
#: to read one. Agreeing that two tickets are duplicates is a judgement that
#: shapes triage, and it is recorded against the person who made it -- so it
#: belongs to whoever is allowed to change the ticket, not to anyone who can
#: see it.
CanDecide = Annotated[RequestContext, Depends(require(Permission.TICKET_UPDATE))]


@router.get(
    "/{ticket_ref}/similar",
    response_model=SimilarTicketsResponse,
    summary="Previously raised tickets that resemble this one",
)
async def similar_tickets(
    ticket_ref: str,
    ctx: CanRead,
    db: ScopedDb,
    limit: Annotated[int, Query(ge=1, le=20)] = 5,
    rerank: Annotated[bool | None, Query(description="Override the reranking flag.")] = None,
    refresh: Annotated[bool, Query(description="Bypass the cache and recompute.")] = False,
) -> SimilarTicketsResponse:
    """Hybrid retrieval over the tenant's own ticket history.

    No LLM is involved. Semantic search finds paraphrases, Postgres full-text
    search finds literal tokens like error codes, and Reciprocal Rank Fusion
    merges the two rankings. Typical latency is one embedding call plus two
    index lookups.

    Returns an empty list when nothing resembles the ticket. That is a real
    answer: most tickets have no near-duplicate, and padding the list with weak
    matches is how a panel like this earns a reputation for being noise.
    """
    tickets = TicketService(db, ctx.tenant)
    ticket = await _resolve(tickets, ticket_ref)

    if not similarity_enabled():
        # 503 rather than an empty 200. An empty list means "nothing similar",
        # and conflating that with "the feature is switched off" makes the
        # frontend unable to say anything honest to the user.
        raise ServiceUnavailableError("Similar-issue search is disabled.")

    try:
        ai = await get_ai_deps()
    except Exception as exc:
        # Ollama or Pinecone unreachable. The ticket page must still render, so
        # this degrades to a 503 on one panel rather than an error on the page.
        logger.warning("similarity unavailable", extra={"error": type(exc).__name__})
        raise ServiceUnavailableError("Similar-issue search is temporarily unavailable.") from exc

    budget = await check_user_rate(
        str(ctx.user_id),
        "similar_issues",
        limit=settings.AI_RATE_LIMIT_PER_MINUTE,
        window_seconds=60,
    )
    if not budget.allowed:
        raise RateLimitError(budget.reason)

    service = IntelligenceService(db, ctx.tenant, ai, Cache())
    result = await service.similar_tickets(
        ticket, limit=limit, with_rerank=rerank, force_refresh=refresh
    )

    # A pending suggestion is what the accept/reject buttons act on, so the
    # response has to carry its id. Looked up rather than returned from the
    # pipeline because a cache hit has no fresh suggestions to hand back -- the
    # rows are still in Postgres, which is the point of storing them there.
    suggestion_ids: dict[uuid.UUID, uuid.UUID] = {}
    if result.rerank_model or result.from_cache:
        for suggestion in await service.suggestions_for(ticket.id):
            if suggestion.related_ticket_id and suggestion.status == "pending":
                suggestion_ids[suggestion.related_ticket_id] = suggestion.id

    return SimilarTicketsResponse(
        ticket_id=ticket.id,
        results=[
            SimilarTicketOut(
                ticket_id=item.ticket_id,
                reference=item.reference,
                title=item.title,
                status=item.status,
                priority=item.priority,
                team_id=item.team_id,
                client_id=item.client_id,
                created_at=item.created_at,
                closed_at=item.closed_at,
                resolution_summary=item.resolution_summary,
                score=round(item.score, 6),
                similarity=(None if item.similarity is None else round(item.similarity, 4)),
                sources=[source.value for source in item.ranks],
                agreed=item.agreed,
                suggestion_id=suggestion_ids.get(item.ticket_id),
                relation=item.relation.value if item.relation else None,
                confidence=item.confidence,
                reason=item.reason,
            )
            for item in result.results
        ],
        total_candidates=result.total_candidates,
        references=list(result.references),
        trace=result.trace,
        degraded=result.degraded,
        blocked_reason=result.blocked_reason,
        grade=result.grade,
        rewrites=result.rewrites,
        rerank_model=result.rerank_model,
        from_cache=result.from_cache,
    )


@router.post(
    "/suggestions/{suggestion_id}/decide",
    response_model=SuggestionOut,
    summary="Accept or reject an AI suggestion",
)
async def decide_suggestion(
    suggestion_id: uuid.UUID,
    payload: SuggestionDecision,
    ctx: CanDecide,
    db: ScopedDb,
) -> SuggestionOut:
    """Record agreement or disagreement with one suggestion.

    Accepting does **not** link the tickets. It records that a person agreed,
    which is a different fact from restructuring the ticket -- linking is a
    ticket-module action with its own permission and its own audit trail. The
    separation is what makes it safe to click.

    Every decision is also a labelled pair for the evaluation set, so this
    endpoint is where the golden set stops being synthetic.
    """
    ai = await get_ai_deps()
    service = IntelligenceService(db, ctx.tenant, ai)
    suggestion = await service.decide_suggestion(
        suggestion_id, accepted=payload.accepted, user_id=ctx.user_id
    )
    return SuggestionOut(
        id=suggestion.id,
        ticket_id=suggestion.ticket_id,
        related_ticket_id=suggestion.related_ticket_id,
        relation=suggestion.relation.value if suggestion.relation else None,
        confidence=suggestion.confidence,
        reason=suggestion.reason,
        model=suggestion.model,
        status=suggestion.status.value,
        decided_at=suggestion.decided_at,
    )


def _looks_like_uuid(value: str) -> bool:
    try:
        uuid.UUID(value)
    except ValueError:
        return False
    return True


async def _resolve(service: TicketService, ticket_ref: str) -> Ticket:
    """Accept a UUID or an ``OS-1042`` reference, as every ticket route does."""
    if _looks_like_uuid(ticket_ref):
        return await service.get(uuid.UUID(ticket_ref))
    return await service.get_by_reference(ticket_ref)


# ------------------------------------------------------------ agent analysis


def _run_payload(run: AiAnalysisRun, steps: list[AiAnalysisStep]) -> AnalysisRunOut:
    """Shape a run for the client.

    The trace and the stop reason are pulled out of the stored report blob
    rather than living in their own columns. They are diagnostics about one
    run, not queryable facts about the system, and a JSONB blob is the right
    home for that -- adding columns for them would be modelling the debugger.
    """
    payload = run.report or {}
    report = None
    if payload:
        fields = {k: v for k, v in payload.items() if not k.startswith("_")}
        if fields:
            report = AnalysisReportOut.model_validate(fields)

    return AnalysisRunOut(
        id=run.id,
        ticket_id=run.ticket_id,
        status=run.status.value,
        report=report,
        partial=run.partial,
        stop_reason=payload.get("_stop_reason") or run.error,
        model=run.model,
        used_fallback_model=run.used_fallback_model,
        turns=run.turns,
        tool_calls=run.tool_calls,
        estimated_cost=run.estimated_cost,
        latency_ms=run.latency_ms,
        helpful=run.helpful,
        steps=[AnalysisStepOut.model_validate(step) for step in steps],
        trace=list(payload.get("_trace", [])),
    )


@router.post(
    "/{ticket_ref}/analysis",
    response_model=AnalysisRunOut,
    status_code=202,
    summary="Queue an AI analysis of this ticket",
)
async def request_analysis(
    ticket_ref: str,
    ctx: CanDecide,
    db: ScopedDb,
) -> AnalysisRunOut:
    """Queue an analysis and return 202 with the run to poll.

    202 rather than 200, because nothing has been analysed yet -- the honest
    status for "accepted, not done". A run takes tens of seconds and several
    LLM calls, so holding the request open would mean a proxy timeout and a
    user with no idea whether anything is happening.

    Requires the ticket-update permission rather than merely read: a run costs
    real money and quota, so it is not something any viewer should be able to
    trigger by loading a page.
    """
    if not settings.AI_ANALYSIS_ENABLED:
        raise ServiceUnavailableError("AI analysis is disabled.")

    budget = await check_user_rate(
        str(ctx.user_id),
        "analysis",
        # Far tighter than the similarity limit: each of these is several LLM
        # calls, not one cached lookup.
        limit=settings.AI_ANALYSIS_LIMIT_PER_HOUR,
        window_seconds=3600,
    )
    if not budget.allowed:
        raise RateLimitError(budget.reason)

    tickets = TicketService(db, ctx.tenant)
    ticket = await _resolve(tickets, ticket_ref)

    run = await AnalysisService(db, ctx.tenant).request(ticket, ctx.user_id)
    return _run_payload(run, [])


@router.get(
    "/{ticket_ref}/analysis",
    response_model=AnalysisRunOut | None,
    summary="The latest analysis for this ticket",
)
async def latest_analysis(
    ticket_ref: str,
    ctx: CanRead,
    db: ScopedDb,
) -> AnalysisRunOut | None:
    """Poll this until `status` is terminal.

    Returns the step log as it grows, which is what makes the wait tolerable:
    the user watches the agent read the ticket, search for duplicates, and
    check a history, rather than watching a spinner.
    """
    tickets = TicketService(db, ctx.tenant)
    ticket = await _resolve(tickets, ticket_ref)

    found = await AnalysisService(db, ctx.tenant).latest(ticket.id)
    if found is None:
        return None
    run, steps = found
    return _run_payload(run, steps)


@router.post(
    "/analysis/{run_id}/rating",
    response_model=AnalysisRunOut,
    summary="Rate an analysis",
)
async def rate_analysis(
    run_id: uuid.UUID,
    payload: AnalysisRating,
    ctx: CanDecide,
    db: ScopedDb,
) -> AnalysisRunOut:
    """Record whether the analysis was useful.

    The only number that says whether the agent is worth its cost. Turns,
    tokens, and latency all describe what it did; this says whether it helped.
    """
    service = AnalysisService(db, ctx.tenant)
    run = await service.rate(run_id, payload.helpful)
    _, steps = await service.get(run_id)
    return _run_payload(run, steps)
