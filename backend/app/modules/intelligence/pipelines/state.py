"""Graph state definitions.

A `TypedDict` rather than a Pydantic model or a bag of arguments, because that
is what LangGraph merges: a node returns a **partial** dict and the framework
applies it over the current state. Two consequences worth being deliberate
about:

* A node may only return keys it actually owns. Returning the whole state
  couples a node to fields it has no business writing, and the next person to
  add a field discovers it the hard way.
* Any field that must *accumulate* rather than be replaced needs a reducer
  annotation. Without one, the last writer wins silently — which for a trace
  list means the trace only ever has one entry.

Declared in Phase B and wired into an actual ``StateGraph`` in Phase C. The
state shape does not change when that happens, which is the point: the nodes
are already written against the contract the graph will use.
"""

from __future__ import annotations

import operator
import uuid
from typing import Annotated, Any, TypedDict

from app.modules.intelligence.schemas.types import (
    FusedCandidate,
    RankedList,
    RetrievalSource,
)


def _merge_rankings(left: list[RankedList], right: list[RankedList]) -> list[RankedList]:
    """Combine ranked lists from retrievers that run in parallel.

    Needed because the semantic and lexical retrieve nodes both write
    ``rankings`` and both run concurrently. Without a reducer, whichever
    finished last would erase the other and the "hybrid" retriever would
    quietly become whichever one is slower.

    Last write wins *per source*, so a re-run after a query rewrite replaces
    that source's list instead of appending a stale one.
    """
    by_source = {ranked.source: ranked for ranked in left}
    for ranked in right:
        by_source[ranked.source] = ranked
    return list(by_source.values())


class SimilarityState(TypedDict, total=False):
    """State for the similar-issues pipeline.

    ``total=False`` because nodes fill it in progressively; requiring every key
    up front would mean constructing placeholder values whose only job is to be
    overwritten.
    """

    # --- inputs -----------------------------------------------------------
    tenant_id: uuid.UUID
    #: The ticket we are finding neighbours for. Excluded from its own results.
    ticket_id: uuid.UUID | None
    query_text: str
    #: Which retrievers to run. A set rather than booleans so the ablation
    #: script exercises the real pipeline with a component removed, instead of
    #: a separate code path that only resembles it.
    sources: frozenset[RetrievalSource]

    # --- produced by nodes ------------------------------------------------
    query_vector: list[float]
    #: Ticket numbers named explicitly in the text ("same as OS-142").
    references: tuple[int, ...]
    #: Resolved ids for those references, pinned above the ranked results.
    pinned: tuple[uuid.UUID, ...]
    rankings: Annotated[list[RankedList], _merge_rankings]
    candidates: list[FusedCandidate]
    #: Raw cosine similarity per ticket, straight from the vector store.
    #:
    #: **Display only. Fusion must never read this.** `RankedList` discards
    #: scores deliberately, because a cosine of 0.72 and a `ts_rank_cd` of 0.09
    #: are not comparable numbers while "third" and "third" are -- that is the
    #: whole reason rank-based fusion needs no weight tuning.
    #:
    #: But the fused score that survives is rank-derived and lands around
    #: 0.016, which is meaningless to a human reading a panel. So the true
    #: similarity is carried alongside, untouched, purely so the UI can say how
    #: close two tickets actually are. Overwritten on a rewrite cycle, which is
    #: correct: the scores shown must belong to the query that produced them.
    vector_scores: dict[uuid.UUID, float]

    # --- corrective loop (Phase C) ----------------------------------------
    #: Whether retrieval was good enough. See `nodes/grade.py`.
    grade: str
    grade_reason: str
    #: How many rewrite cycles have run. The cycle's only termination
    #: guarantee, and the reason a cyclic graph cannot loop forever.
    rewrites: int
    #: The query as originally given. Kept so a rewrite is always applied to
    #: the human's words rather than to the previous rewrite -- otherwise two
    #: cycles compound into a paraphrase of a paraphrase.
    original_query: str

    # --- rerank (Phase C) -------------------------------------------------
    #: Candidate text, hydrated from Postgres for the reranker to read.
    #: ticket_id -> {reference, title, description, status, closed_at}
    catalogue: dict[uuid.UUID, dict[str, Any]]
    judgements: list[Any]
    rerank_model: str
    rerank_cost: float
    #: Judgements naming a ticket that was never in the candidate set. Dropped,
    #: and counted -- a rising number means the prompt or the model is drifting.
    ungrounded_citations: int

    # --- guardrails (Phase C) ---------------------------------------------
    #: Set when an input guardrail refused the request. The graph routes
    #: straight to the end; nothing downstream has to check.
    blocked: bool
    blocked_reason: str

    # --- observability ----------------------------------------------------
    #: Which nodes ran. Graph tests assert on the *path*, not the output —
    #: "did weak retrieval actually trigger the rewrite" is a deterministic
    #: question, and this is what makes it answerable.
    trace: Annotated[list[str], operator.add]
