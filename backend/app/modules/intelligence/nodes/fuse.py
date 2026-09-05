"""Fusion node.

Thin by design: the interesting logic is a pure function in
``domain/fusion.py`` where it can be tested with lists of integers. A node's
job is to move data between the state and a function, not to hold the
algorithm.
"""

from __future__ import annotations

from app.modules.intelligence.domain.deps import RetrievalDeps
from app.modules.intelligence.domain.fusion import (
    cascade,
    cascade_leader,
    reciprocal_rank_fusion,
    retriever_weights,
)
from app.modules.intelligence.domain.types import FusionMode
from app.modules.intelligence.graphs.state import SimilarityState


async def fuse_candidates(state: SimilarityState, deps: RetrievalDeps) -> dict[str, object]:
    """Combine every retriever's ranking into one ordered candidate list.

    The score floor is applied *after* fusion rather than per retriever. A
    document that ranked 40th semantically and 40th lexically is a better
    candidate than one that ranked 8th in a single list, and a per-retriever
    cutoff would have discarded the first before fusion ever saw the
    agreement.
    """
    rankings = state.get("rankings", [])
    pinned = state.get("pinned", ())
    query_text = state["query_text"]
    lead = cascade_leader(rankings)

    if deps.fusion_mode is FusionMode.CASCADE:
        fused = cascade(
            rankings,
            leader=lead,
            k=deps.rrf_k,
            limit=deps.candidate_limit,
            pinned=pinned,
        )
    else:
        fused = reciprocal_rank_fusion(
            rankings,
            k=deps.rrf_k,
            limit=deps.candidate_limit,
            pinned=pinned,
            weights=(
                retriever_weights(query_text, secondary=deps.secondary_weight)
                if deps.fusion_mode is FusionMode.WEIGHTED_RRF
                else None
            ),
        )

    kept = [
        candidate
        for candidate in fused
        # Pinned references carry a synthetic score of 1.0 and are facts rather
        # than estimates, so the floor does not apply to them.
        if candidate.score >= deps.score_floor
    ]
    return {
        "candidates": kept,
        "trace": [
            f"fuse:{len(kept)}/{sum(len(r.ticket_ids) for r in rankings)}",
            f"fuse:{deps.fusion_mode.value}:lead={lead.value}",
        ],
    }
