"""The embedding node.

No vendor imports here — the provider arrives through ``deps``, which is what
lets these run in a unit test with a fake that returns hashed vectors.
"""

from __future__ import annotations

from app.modules.intelligence.pipelines.state import SimilarityState
from app.modules.intelligence.retrieval.deps import RetrievalDeps
from app.modules.intelligence.schemas.types import RetrievalSource


async def embed_query(state: SimilarityState, deps: RetrievalDeps) -> dict[str, object]:
    """Embed the query text.

    Calls ``embed_query``, never ``embed_documents``. Qwen3-Embedding is
    asymmetric — queries take a task-instruction prefix, documents do not — and
    using the wrong one produces no error at all, just measurably worse recall.
    The port has two methods precisely so this cannot be got wrong silently.
    """
    if RetrievalSource.SEMANTIC not in state.get("sources", frozenset()):
        # The ablation script removes semantic retrieval; skipping the embed
        # call as well is what makes its "lexical only" row honest about cost.
        return {"trace": ["embed:skipped"]}

    vector = await deps.embeddings.embed_query(state["query_text"])
    return {"query_vector": vector, "trace": ["embed"]}
