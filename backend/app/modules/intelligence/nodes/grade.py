"""Grading: deciding whether retrieval was good enough.

The node that makes this *corrective* RAG rather than plain RAG. Plain RAG
retrieves once and hopes; corrective RAG checks its own retrieval and can go
back for more.

**No LLM here, deliberately.** Asking a model "are these results good?" costs a
call, adds a second of latency, and answers a question that two counts already
answer. The grade is a function of how many candidates cleared the score floor
and whether the retrievers agreed — both free, both deterministic, both
explainable in a log line.
"""

from __future__ import annotations

from enum import StrEnum

from app.core.logging import get_logger
from app.modules.intelligence.domain.deps import RetrievalDeps
from app.modules.intelligence.domain.types import RetrievalSource
from app.modules.intelligence.graphs.state import SimilarityState

logger = get_logger(__name__)

#: Enough candidates that a reranker has something to choose between. Below
#: this, reranking is spending an LLM call to reorder two rows.
MIN_CANDIDATES = 3

#: A pinned exact match is worth more than any number of ranked guesses. When
#: one exists, retrieval has already succeeded and a rewrite cycle would be
#: spending a call to improve an answer we are already certain of.
CERTAIN_SOURCES = frozenset({RetrievalSource.REFERENCE})


class Grade(StrEnum):
    """What to do next."""

    #: Enough good candidates. Proceed.
    GOOD = "good"
    #: Thin, but a rewrite has not been tried yet. Worth one cycle.
    WEAK = "weak"
    #: Nothing worth showing, and rewriting has been tried. Stop, and say so.
    #: "No similar issues found" is a real answer — see the module docstring of
    #: `nodes/rerank.py` for why padding the list is worse than an empty one.
    EMPTY = "empty"


async def grade_candidates(state: SimilarityState, deps: RetrievalDeps) -> dict[str, object]:
    """Score the retrieval, not the tickets.

    Returns a grade plus the reasoning behind it, because "why did this
    escalate to a rewrite" is the first question asked when the pipeline
    behaves oddly, and reconstructing it later from candidate counts is
    guesswork.
    """
    candidates = state.get("candidates", [])
    rewrites = state.get("rewrites", 0)

    certain = [c for c in candidates if CERTAIN_SOURCES & set(c.ranks)]
    agreed = [c for c in candidates if c.agreed]

    if certain:
        grade = Grade.GOOD
        why = f"{len(certain)} exact match(es)"
    elif len(candidates) >= MIN_CANDIDATES and agreed:
        grade = Grade.GOOD
        why = f"{len(candidates)} candidates, {len(agreed)} agreed by both retrievers"
    elif len(candidates) >= MIN_CANDIDATES:
        # Enough candidates but no cross-retriever agreement. Good enough to
        # rerank, not good enough to be confident about -- the reranker will
        # sort it out, and that is cheaper than a rewrite plus a rerank.
        grade = Grade.GOOD
        why = f"{len(candidates)} candidates, none agreed"
    elif candidates and rewrites >= deps.max_rewrites:
        grade = Grade.GOOD
        why = f"{len(candidates)} candidates and rewrites exhausted; showing what we have"
    elif rewrites < deps.max_rewrites:
        grade = Grade.WEAK
        why = f"only {len(candidates)} candidates; rewriting (attempt {rewrites + 1})"
    else:
        grade = Grade.EMPTY
        why = "nothing above the floor after rewriting"

    logger.info("retrieval graded", extra={"grade": grade.value, "reason": why})
    return {"grade": grade.value, "grade_reason": why, "trace": [f"grade:{grade.value}"]}


def route_after_grading(state: SimilarityState) -> str:
    """The conditional edge.

    A separate pure function from the node that produced the grade, which is
    what lets a test assert the routing decision without running the graph.
    Returns a node name; the graph maps it in `add_conditional_edges`.
    """
    grade = state.get("grade", Grade.GOOD.value)
    if grade == Grade.WEAK.value:
        return "rewrite"
    if grade == Grade.EMPTY.value:
        return "finish"
    return "rerank"
