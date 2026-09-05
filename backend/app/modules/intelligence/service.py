"""The AI module's only public door.

Routers and other modules import ``IntelligenceService`` and nothing else — the
same rule every other module in this project follows. Nothing outside this
package ever imports ``graphs/`` or ``nodes/``, which is what keeps the
internals free to change.
"""

from __future__ import annotations

import uuid
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.cache import Cache, CacheKey
from app.core.config import settings
from app.core.errors import NotFoundError
from app.core.logging import get_logger
from app.modules.intelligence.cache import from_cache, to_cache
from app.modules.intelligence.deps import AiDeps, build_retrieval_deps
from app.modules.intelligence.llm.registry import ModelRegistry
from app.modules.intelligence.models import AiSuggestion, Relation
from app.modules.intelligence.pipelines.similarity import run_similarity
from app.modules.intelligence.pipelines.state import SimilarityState
from app.modules.intelligence.query.parsing import build_query_text
from app.modules.intelligence.repository import SuggestionRepository
from app.modules.intelligence.schemas.types import (
    FusedCandidate,
    FusionMode,
    RetrievalSource,
    SimilarityResult,
    SimilarTicket,
)
from app.modules.tickets.models import Ticket

logger = get_logger(__name__)

#: How much of the resolution notes to surface. Enough to see the shape of the
#: fix; not so much that the panel becomes a wall of text nobody reads.
RESOLUTION_SUMMARY_CHARS = 320

#: Every retriever, which is the production configuration. The ablation script
#: passes subsets of this to measure what each one contributes.
ALL_SOURCES = frozenset({RetrievalSource.SEMANTIC, RetrievalSource.LEXICAL})

#: Placeholder for a candidate the reranker did not judge, so the lookup
#: below has one shape rather than a None check at three call sites.
_NO_VERDICT: tuple[Relation | None, float | None, str | None] = (None, None, None)


class IntelligenceService:
    def __init__(
        self,
        session: AsyncSession,
        tenant_id: uuid.UUID,
        ai: AiDeps,
        cache: Cache | None = None,
    ) -> None:
        self._session = session
        self._tenant_id = tenant_id
        self._ai = ai
        self._cache = cache
        self._suggestions = SuggestionRepository(session)

    async def similar_tickets(
        self,
        ticket: Ticket,
        *,
        limit: int = 5,
        sources: frozenset[RetrievalSource] = ALL_SOURCES,
        with_rerank: bool | None = None,
        force_refresh: bool = False,
    ) -> SimilarityResult:
        """Find tickets that look like this one.

        Uses the ticket's *own* text as the query, so this is a
        document-to-document search dressed as a query-to-document one. That is
        why the query is built by the same convention as the stored embedding
        text — title first, description second, comments and resolution notes
        excluded. A query assembled differently from the documents is the most
        common reason a hybrid retriever quietly under-performs.

        Deliberately called only when a ticket detail page is opened, never
        while one is being typed: at creation time there is nothing to compare
        against yet, and a suggestion panel that reacts to every keystroke is
        both expensive and distracting.
        """
        state: SimilarityState = {
            "tenant_id": self._tenant_id,
            "ticket_id": ticket.id,
            "query_text": build_query_text(ticket.title, ticket.description),
            "sources": sources,
        }

        cache_key = CacheKey.similar_tickets(self._tenant_id, ticket.id, ticket.version)
        if self._cache is not None and not force_refresh:
            cached = await self._cache.get_json(cache_key)
            if cached is not None:
                return from_cache(cached, limit)

        rerank = settings.AI_RERANK_ENABLED if with_rerank is None else with_rerank
        deps = build_retrieval_deps(
            self._session,
            self._ai,
            registry=ModelRegistry(self._session, self._tenant_id) if rerank else None,
            rerank_enabled=rerank,
        )

        # One graph serves both configurations. With `rerank` false the LLM
        # nodes see no registry and return the fused order unchanged, so the
        # retrieval-only path is the same code and not a second implementation
        # that has to be kept in step.
        final = await run_similarity(state, deps)

        if final.get("blocked"):
            return SimilarityResult(
                results=[],
                total_candidates=0,
                references=(),
                trace=final.get("trace", []),
                degraded=False,
                blocked_reason=final.get("blocked_reason", ""),
            )

        candidates = final.get("candidates", [])
        judgements = final.get("judgements", [])
        trace = final.get("trace", [])
        catalogue = final.get("catalogue", {})

        ordered = _apply_judgements(candidates, judgements, catalogue)
        hydrated = await self._hydrate(
            ordered[:limit], judgements, catalogue, final.get("vector_scores")
        )

        result = SimilarityResult(
            results=hydrated,
            total_candidates=len(candidates),
            references=final.get("references", ()),
            trace=trace,
            # A retriever that returned nothing when it was asked to run means
            # results came from the other half alone. Worth telling the caller:
            # the answer is still useful but it is not the full pipeline.
            degraded=any("no-vector" in entry or "empty-query" in entry for entry in trace),
            grade=final.get("grade", ""),
            rewrites=final.get("rewrites", 0),
            rerank_model=final.get("rerank_model"),
            estimated_cost=final.get("rerank_cost", 0.0),
        )

        if judgements:
            # Only judged results become stored suggestions. An unjudged fused
            # ordering is a ranking, not a claim about a relationship, and
            # storing it as one would fill the accuracy signal with rows nobody
            # was ever asked to agree with.
            await self._persist_suggestions(ticket, hydrated, final.get("rerank_model"))

        if self._cache is not None:
            await self._cache.set_json(
                cache_key, to_cache(result), ttl_seconds=settings.AI_CACHE_TTL_SECONDS
            )
        return result

    async def similar_to_text(
        self,
        title: str,
        description: str | None = None,
        *,
        limit: int = 5,
        exclude: uuid.UUID | None = None,
        sources: frozenset[RetrievalSource] = ALL_SOURCES,
        with_rerank: bool | None = None,
    ) -> SimilarityResult:
        """Same pipeline, driven by free text rather than an existing ticket.

        Not exposed on the ticket-create form — that was an explicit product
        decision. It exists because the evaluation harness needs to query with
        held-out text, and Phase E's agent needs it as a tool. One pipeline
        serving all three is the reason the retrieval nodes were written as
        nodes.
        """
        state: SimilarityState = {
            "tenant_id": self._tenant_id,
            "ticket_id": exclude,
            "query_text": build_query_text(title, description),
            "sources": sources,
        }
        rerank = settings.AI_RERANK_ENABLED if with_rerank is None else with_rerank
        deps = build_retrieval_deps(
            self._session,
            self._ai,
            registry=ModelRegistry(self._session, self._tenant_id) if rerank else None,
            rerank_enabled=rerank,
        )
        final = await run_similarity(state, deps)

        candidates = final.get("candidates", [])
        judgements = final.get("judgements", [])
        catalogue = final.get("catalogue", {})
        ordered = _apply_judgements(candidates, judgements, catalogue)
        return SimilarityResult(
            results=await self._hydrate(
                ordered[:limit], judgements, catalogue, final.get("vector_scores")
            ),
            total_candidates=len(candidates),
            references=final.get("references", ()),
            trace=final.get("trace", []),
            degraded=False,
            grade=final.get("grade", ""),
            rewrites=final.get("rewrites", 0),
            rerank_model=final.get("rerank_model"),
            estimated_cost=final.get("rerank_cost", 0.0),
        )

    async def candidate_ids(
        self,
        title: str,
        description: str | None,
        *,
        limit: int,
        exclude: uuid.UUID | None,
        sources: frozenset[RetrievalSource],
        rrf_k: int | None = None,
        fusion_mode: FusionMode | None = None,
        exact_probe: bool | None = None,
    ) -> list[uuid.UUID]:
        """Ranked ids only, no hydration.

        The evaluation harness runs this thousands of times and only needs the
        ranking; loading full ticket rows for each would dominate the runtime
        and measure the ORM rather than the retriever.
        """
        state: SimilarityState = {
            "tenant_id": self._tenant_id,
            "ticket_id": exclude,
            "query_text": build_query_text(title, description),
            "sources": sources,
        }
        deps = build_retrieval_deps(
            self._session,
            self._ai,
            candidate_limit=limit,
            rrf_k=rrf_k,
            fusion_mode=fusion_mode,
            exact_probe=exact_probe,
            # No score floor during evaluation: recall@20 must be measured over
            # what retrieval *reached*, not over what a display threshold let
            # through. Otherwise the floor silently becomes part of the metric.
            score_floor=0.0,
        )
        final = await run_similarity(state, deps)
        return [candidate.ticket_id for candidate in final.get("candidates", [])]

    # ------------------------------------------------------------- hydration

    async def _hydrate(
        self,
        candidates: list[FusedCandidate],
        judgements: list[Any],
        catalogue: dict[uuid.UUID, dict[str, Any]],
        vector_scores: dict[uuid.UUID, float] | None = None,
    ) -> list[SimilarTicket]:
        """Load the display fields from Postgres, preserving fused order.

        Ticket text is read from Postgres and never from vector-store
        metadata. Pinecone holds ids, vectors, and filter metadata only, so
        sensitive content lives in exactly one place — one store to secure,
        back up, and satisfy a deletion request against.
        """
        if not candidates:
            return []

        # reference -> (relation, confidence, reason), so a judged candidate
        # carries the model's verdict through to the response.
        verdicts = {j.reference: (j.relation, j.confidence, j.reason) for j in judgements}

        ids = [candidate.ticket_id for candidate in candidates]
        rows = await self._session.execute(
            select(Ticket).where(Ticket.id.in_(ids), Ticket.deleted_at.is_(None))
        )
        by_id = {ticket.id: ticket for ticket in rows.scalars().all()}

        out: list[SimilarTicket] = []
        for candidate in candidates:
            ticket = by_id.get(candidate.ticket_id)
            if ticket is None:
                # In Pinecone but not in Postgres: a ticket deleted since the
                # last reconcile. Skipped rather than surfaced as a broken row,
                # and logged because a growing count means the reconciler is
                # not running.
                logger.warning(
                    "similar candidate missing from postgres",
                    extra={"ticket_id": str(candidate.ticket_id)},
                )
                continue
            out.append(
                SimilarTicket(
                    ticket_id=ticket.id,
                    reference=ticket.reference,
                    title=ticket.title,
                    status=ticket.status.value,
                    priority=ticket.priority.value,
                    team_id=ticket.team_id,
                    client_id=ticket.client_id,
                    created_at=ticket.created_at,
                    closed_at=ticket.closed_at,
                    resolution_summary=_summarise(ticket.resolution_notes),
                    score=candidate.score,
                    # None, not 0.0, when only keyword search found it. There is
                    # no vector comparison to report, and a zero would read as
                    # "the meanings are unrelated" rather than "not measured".
                    similarity=(vector_scores or {}).get(candidate.ticket_id),
                    ranks=candidate.ranks,
                    agreed=candidate.agreed,
                    relation=verdicts.get(ticket.reference, _NO_VERDICT)[0],
                    confidence=verdicts.get(ticket.reference, _NO_VERDICT)[1],
                    reason=verdicts.get(ticket.reference, _NO_VERDICT)[2],
                )
            )
        return out

    async def _persist_suggestions(
        self,
        ticket: Ticket,
        results: list[SimilarTicket],
        model: str | None,
    ) -> None:
        """Record judged results as pending suggestions.

        Written in the request's transaction, so a suggestion shown to a user
        is a suggestion that exists -- the accept button cannot 404 because the
        row was never committed.
        """
        judged = [item for item in results if item.relation is not None]
        if not judged:
            return
        await self._suggestions.replace_similar(
            self._tenant_id,
            ticket.id,
            model,
            [
                {
                    "related_ticket_id": item.ticket_id,
                    "relation": item.relation,
                    "confidence": item.confidence,
                    "reason": item.reason,
                    "payload": {
                        "score": item.score,
                        # The similarity that produced this suggestion, kept so
                        # accepted/rejected pairs can later be analysed against
                        # the score that generated them. In `payload` because
                        # the repository only reads the keys it names -- a
                        # top-level key it does not know about is dropped
                        # without complaint.
                        "similarity": item.similarity,
                        "sources": [source.value for source in item.ranks],
                    },
                }
                for item in judged
            ],
        )

    async def decide_suggestion(
        self,
        suggestion_id: uuid.UUID,
        *,
        accepted: bool,
        user_id: uuid.UUID,
    ) -> AiSuggestion:
        """Record a human verdict on a suggestion.

        Accepting does **not** write ``tickets.duplicate_of_id``. Linking two
        tickets is a ticket-module action with its own permission and its own
        audit entry; this records agreement, which is a different fact. Keeping
        them separate is what lets someone say "yes, that is the same bug"
        without also asserting authority to restructure the ticket.
        """
        suggestion = await self._suggestions.get(suggestion_id)
        if suggestion is None:
            raise NotFoundError("Suggestion not found.")
        decided = await self._suggestions.decide(suggestion, accepted=accepted, user_id=user_id)
        logger.info(
            "suggestion decided",
            extra={
                "suggestion_id": str(suggestion_id),
                "accepted": accepted,
                "relation": decided.relation.value if decided.relation else None,
                "confidence": decided.confidence,
                "model": decided.model,
            },
        )
        return decided

    async def suggestions_for(self, ticket_id: uuid.UUID) -> list[AiSuggestion]:
        return await self._suggestions.for_ticket(ticket_id)

    async def acceptance_rate(self, days: int = 30) -> dict[str, int]:
        return await self._suggestions.acceptance_rate(self._tenant_id, days)


def _apply_judgements(
    candidates: list[FusedCandidate],
    judgements: list[Any],
    catalogue: dict[uuid.UUID, dict[str, Any]],
) -> list[FusedCandidate]:
    """Reorder candidates by the reranker's verdict, and drop what it discarded.

    The reranker returns *fewer* judgements than candidates -- anything it
    called unrelated, or judged below the confidence floor, is gone. So this
    both reorders and filters, and the fused order survives only for candidates
    the model did not mention.

    With no judgements the fused order is returned untouched, which is the
    retrieval-only path.
    """
    if not judgements:
        return candidates

    rank_by_reference = {j.reference: index for index, j in enumerate(judgements)}
    judged: list[tuple[int, FusedCandidate]] = []
    for candidate in candidates:
        entry = catalogue.get(candidate.ticket_id)
        if entry is None:
            continue
        position = rank_by_reference.get(entry["reference"])
        if position is None:
            # The model dropped it. Trusting that is the point of reranking --
            # keeping it "just in case" would mean the confidence floor and the
            # unrelated verdict had no effect on what anyone sees.
            continue
        judged.append((position, candidate))

    judged.sort(key=lambda pair: pair[0])
    return [candidate for _, candidate in judged]


def _summarise(notes: str | None) -> str | None:
    if not notes:
        return None
    text = notes.strip()
    if len(text) <= RESOLUTION_SUMMARY_CHARS:
        return text
    return text[:RESOLUTION_SUMMARY_CHARS].rstrip() + "…"


def similarity_enabled() -> bool:
    """Whether the feature should answer at all.

    Two flags, because they turn off different things: ``AI_ENABLED`` is the
    kill switch for the whole layer, ``AI_SIMILARITY_ENABLED`` disables this
    feature while leaving embedding and the rest running.
    """
    return settings.AI_ENABLED and settings.AI_SIMILARITY_ENABLED
