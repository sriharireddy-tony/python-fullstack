"""Hydration and the input guard.

Both are small, and both sit at a boundary: one loads the text the LLM will
read, the other decides what the LLM is allowed to read.
"""

from __future__ import annotations

from app.core.logging import get_logger
from app.modules.intelligence.pipelines.state import SimilarityState
from app.modules.intelligence.retrieval.deps import RetrievalDeps
from app.modules.intelligence.security.input import guard_input

logger = get_logger(__name__)


async def guard_query(state: SimilarityState, deps: RetrievalDeps) -> dict[str, object]:
    """Bound, redact, and screen the query before anything else runs.

    First node in the graph, so no later node has to wonder whether the text it
    is holding has been through this. That is the argument for a guardrail as a
    *node* rather than a helper called wherever someone remembers: the graph
    topology makes it unskippable.

    PII is redacted here even when only the local model will see the text.
    Uniformity is the point — a redaction that depends on which model happens
    to serve the role is one config change away from leaking, and the whole
    reason the registry can fall back between hosted and local is that the
    layers above do not know which one answered.
    """
    guarded = guard_input(state["query_text"], feature="similar_issues")

    if guarded.blocked:
        return {
            "blocked": True,
            "blocked_reason": guarded.reason,
            "trace": ["guard:blocked"],
        }

    marks = []
    if guarded.redactions:
        marks.append(f"redacted={sum(guarded.redactions.values())}")
    if guarded.injection_markers:
        marks.append(f"injection_markers={len(guarded.injection_markers)}")
    if guarded.truncated:
        marks.append("truncated")

    return {
        "query_text": guarded.text,
        "original_query": guarded.text,
        "blocked": False,
        "trace": [f"guard:ok{':' + ','.join(marks) if marks else ''}"],
    }


async def hydrate_candidates(state: SimilarityState, deps: RetrievalDeps) -> dict[str, object]:
    """Load candidate text from Postgres for the reranker to read.

    Runs after fusion and before reranking, so exactly the surviving candidates
    are fetched — hydrating before fusion would load fifty rows to use twenty.

    Text comes from Postgres and never from vector-store metadata. Pinecone holds
    ids, vectors, and filter metadata only, which keeps ticket content in one
    place: one store to secure, back up, and satisfy a deletion request against.
    """
    candidates = state.get("candidates", [])
    if not candidates or deps.catalogue is None:
        return {"trace": ["hydrate:skipped"]}

    catalogue = await deps.catalogue.fetch([c.ticket_id for c in candidates])
    missing = len(candidates) - len(catalogue)
    if missing:
        # In the vector store but not in Postgres: a ticket deleted since the
        # last reconcile. Logged because a growing count means the reconciler
        # is not running.
        logger.warning("candidates missing from postgres", extra={"count": missing})

    return {
        "catalogue": catalogue,
        "trace": [f"hydrate:{len(catalogue)}"],
    }
