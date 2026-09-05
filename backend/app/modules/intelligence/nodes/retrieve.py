"""Retrieval nodes — one per retriever.

Two nodes rather than one that does both, because they are independent: they
run concurrently, they fail independently, and the ablation script disables one
without touching the other. A single ``retrieve`` node doing both would make
all three of those awkward.

Both write to ``rankings``, which is why that field has a reducer keyed by
source — see ``graphs/state.py``.
"""

from __future__ import annotations

from app.core.logging import get_logger
from app.modules.intelligence.domain.deps import RetrievalDeps
from app.modules.intelligence.domain.fusion import rank_only
from app.modules.intelligence.domain.query import prepare_lexical_text
from app.modules.intelligence.domain.types import RankedList, RetrievalSource
from app.modules.intelligence.graphs.state import SimilarityState

logger = get_logger(__name__)


async def retrieve_semantic(state: SimilarityState, deps: RetrievalDeps) -> dict[str, object]:
    """Vector search in the tenant's own Chroma collection.

    Finds the paraphrases: "salary slip shows no data for contract staff"
    against "Payslip PDF generates blank for contractors" shares almost no
    words, and lexical search scores it near zero.
    """
    if RetrievalSource.SEMANTIC not in state.get("sources", frozenset()):
        return {"trace": ["retrieve:semantic:skipped"]}

    vector = state.get("query_vector")
    if not vector:
        # The embed node was skipped or failed. Returning an empty ranking
        # rather than raising keeps the other half of the hybrid working: a
        # degraded result beats no result on a page a human is already reading.
        logger.warning("semantic retrieval skipped: no query vector")
        return {
            "rankings": [RankedList(source=RetrievalSource.SEMANTIC, ticket_ids=())],
            "trace": ["retrieve:semantic:no-vector"],
        }

    own = state.get("ticket_id")
    hits = await deps.store.search(
        state["tenant_id"],
        vector,
        limit=deps.semantic_limit,
        exclude={own} if own is not None else None,
    )
    ranked = rank_only([(hit.ticket_id, hit.score) for hit in hits], RetrievalSource.SEMANTIC)
    return {"rankings": [ranked], "trace": [f"retrieve:semantic:{len(ranked.ticket_ids)}"]}


async def retrieve_lexical(state: SimilarityState, deps: RetrievalDeps) -> dict[str, object]:
    """Postgres full-text search over the generated ``search_vector``.

    Earns its place by catching exactly what embeddings are worst at: the
    literal token. Error codes, stack-trace fragments, client-specific
    identifiers, and rare product names all get flattened by an embedding
    model into "something about payroll", while ``ts_rank_cd`` matches them
    precisely.

    It is also free — the column and its GIN index already exist for the
    ticket list's search box.
    """
    if RetrievalSource.LEXICAL not in state.get("sources", frozenset()):
        return {"trace": ["retrieve:lexical:skipped"]}

    lexical_text = prepare_lexical_text(state["query_text"])
    if not lexical_text:
        return {
            "rankings": [RankedList(source=RetrievalSource.LEXICAL, ticket_ids=())],
            "trace": ["retrieve:lexical:empty-query"],
        }

    own = state.get("ticket_id")
    hits = await deps.lexical.search(
        lexical_text,
        limit=deps.lexical_limit,
        exclude={own} if own is not None else None,
    )
    ranked = rank_only([(hit.ticket_id, hit.score) for hit in hits], RetrievalSource.LEXICAL)
    return {"rankings": [ranked], "trace": [f"retrieve:lexical:{len(ranked.ticket_ids)}"]}
