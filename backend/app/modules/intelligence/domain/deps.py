"""What a node is allowed to reach for.

Lives in ``domain/`` and holds **only port types plus plain numbers**, so it
imports no vendor package. That matters more than it looks: if this dataclass
were defined next to the wiring in ``deps.py``, every node importing it would
transitively import ``chromadb``, and the CI grep that checks for vendor
imports in ``nodes/`` would pass while the layering had already collapsed.

The construction of the real adapters lives in ``deps.py``. This file is the
shape; that file is the wiring.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from app.modules.intelligence.domain.types import FusionMode
from app.modules.intelligence.ports import (
    EmbeddingProvider,
    LexicalSearch,
    TicketCatalogue,
    VectorStore,
)

if TYPE_CHECKING:  # pragma: no cover - import cycle only matters to the checker
    from app.modules.intelligence.registry import ModelRegistry


@dataclass(frozen=True, slots=True)
class RetrievalDeps:
    """Everything the retrieval nodes need, injected.

    Passed as an argument rather than read from a global, so a test constructs
    one with fakes and the whole pipeline runs with no Ollama, no Chroma, and
    no database. The knobs are values here rather than reads of ``settings``
    inside nodes for the same reason — the ablation script varies them per run.
    """

    embeddings: EmbeddingProvider
    store: VectorStore
    #: Bound to a tenant-scoped session, so row-level security is what isolates
    #: it. There is no tenant argument to forget.
    lexical: LexicalSearch

    #: How deep each retriever goes before fusion. Larger than the number of
    #: results shown, deliberately: fusion can only reorder what it is given,
    #: so a document neither list reached is unreachable no matter how good the
    #: reranker is later.
    semantic_limit: int = 50
    lexical_limit: int = 50
    #: Rank-flattening constant for RRF.
    rrf_k: int = 60
    #: How many fused candidates to carry forward.
    candidate_limit: int = 20
    #: Fused-score floor. An RRF score below this means the ticket appeared
    #: deep in exactly one list, which is indistinguishable from noise.
    score_floor: float = 0.0
    #: How the two rankings become one. A setting rather than a constant so the
    #: ablation measures every option through the *shipped* code path -- an
    #: ablation with its own copy of the algorithm measures the copy.
    fusion_mode: FusionMode = FusionMode.CASCADE
    #: Multiplier for the non-leading retriever, used by weighted RRF only.
    #: Never 0.0: down-weighting keeps its candidates reachable.
    secondary_weight: float = 0.5
    #: Whether literal identifiers are looked up exactly and pinned. A switch
    #: only so the ablation can measure what the probe contributes -- with it
    #: on, every configuration answers identifier queries perfectly and the
    #: table stops saying anything about the retrievers underneath.
    exact_probe: bool = True

    # --- Phase C: the LLM stages ------------------------------------------
    #: Reads candidate text for the reranker. Session-bound, like `lexical`.
    catalogue: TicketCatalogue | None = None
    #: ``None`` means "retrieval only" -- a legitimate configuration, not a
    #: broken one. Every LLM node checks for it and degrades to the fused
    #: order, which is what the retrieval-only endpoint shipped in Phase B.
    registry: ModelRegistry | None = None
    #: Reranking costs an LLM call per query, so it is opt-in.
    rerank_enabled: bool = False
    #: Judgements below this are dropped rather than shown.
    confidence_floor: float = 0.55
    #: Cap on rewrite cycles. This is what makes a cyclic graph terminate.
    max_rewrites: int = 2
