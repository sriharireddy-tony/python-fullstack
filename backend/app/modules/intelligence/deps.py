"""Dependency wiring for the AI layer.

One place that constructs adapters, so every node, graph, and script receives
them injected rather than importing a vendor client. That is what keeps the
layers above testable with fakes and free of vendor imports.
"""

from __future__ import annotations

from dataclasses import dataclass
from functools import lru_cache

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.logging import get_logger
from app.modules.intelligence.embeddings.embedder import OllamaEmbeddingProvider
from app.modules.intelligence.lexical.postgres_fts import PostgresLexicalSearch
from app.modules.intelligence.llm.registry import ModelRegistry
from app.modules.intelligence.ports import EmbeddingProvider, VectorStore
from app.modules.intelligence.retrieval.catalogue import PostgresTicketCatalogue
from app.modules.intelligence.retrieval.deps import RetrievalDeps
from app.modules.intelligence.schemas.types import FusionMode
from app.modules.intelligence.vectordb.pinecone_store import PineconeVectorStore

logger = get_logger(__name__)

__all__ = ["AiDeps", "RetrievalDeps", "ai_health", "build_retrieval_deps", "get_ai_deps"]


@dataclass(slots=True)
class AiDeps:
    """What every node and service needs from the outside world.

    Passed explicitly rather than imported, so a test constructs one with
    fakes and the whole graph runs with no Ollama, no Pinecone, and no API key.
    """

    embeddings: EmbeddingProvider
    store: VectorStore


@lru_cache(maxsize=1)
def _embeddings() -> OllamaEmbeddingProvider:
    return OllamaEmbeddingProvider()


@lru_cache(maxsize=1)
def _store() -> PineconeVectorStore:
    return PineconeVectorStore()


async def get_ai_deps() -> AiDeps:
    """Build the dependency bundle, warming the embedding model on first use.

    `warm()` probes the dimension — which is the authority for the Pinecone
    index width — and loads the model into Ollama's memory, so the first real
    request does not pay the several-second model-load cost and look broken.
    """
    embeddings = _embeddings()
    await embeddings.warm()
    return AiDeps(embeddings=embeddings, store=_store())


def build_retrieval_deps(
    session: AsyncSession,
    ai: AiDeps,
    *,
    semantic_limit: int | None = None,
    lexical_limit: int | None = None,
    candidate_limit: int | None = None,
    rrf_k: int | None = None,
    score_floor: float | None = None,
    fusion_mode: FusionMode | None = None,
    exact_probe: bool | None = None,
    registry: ModelRegistry | None = None,
    rerank_enabled: bool | None = None,
) -> RetrievalDeps:
    """Bind the app-scoped adapters to a request-scoped session.

    The split matters: the embedding provider and vector store are process-wide
    and cached, while lexical search must be bound to *this request's*
    tenant-scoped session, because row-level security is what isolates it.
    Caching a session-bound object would serve one tenant's rows to another.

    The knobs default from settings but are overridable, so the ablation script
    can vary retrieval depth without editing configuration.
    """
    return RetrievalDeps(
        embeddings=ai.embeddings,
        store=ai.store,
        lexical=PostgresLexicalSearch(session),
        semantic_limit=semantic_limit or settings.RETRIEVAL_SEMANTIC_LIMIT,
        lexical_limit=lexical_limit or settings.RETRIEVAL_LEXICAL_LIMIT,
        candidate_limit=candidate_limit or settings.RETRIEVAL_CANDIDATES,
        rrf_k=rrf_k or settings.RRF_K,
        score_floor=settings.SIMILARITY_FLOOR if score_floor is None else score_floor,
        fusion_mode=fusion_mode or FusionMode(settings.FUSION_MODE),
        exact_probe=settings.EXACT_IDENTIFIER_PROBE if exact_probe is None else exact_probe,
        secondary_weight=settings.RRF_SECONDARY_WEIGHT,
        # Session-bound, like `lexical`: the reranker reads ticket text through
        # the tenant-scoped session, so row-level security is what keeps it
        # inside one tenant and there is no filter to forget.
        catalogue=PostgresTicketCatalogue(session),
        registry=registry,
        rerank_enabled=(settings.AI_RERANK_ENABLED if rerank_enabled is None else rerank_enabled),
        confidence_floor=settings.RERANK_CONFIDENCE_FLOOR,
        max_rewrites=settings.MAX_QUERY_REWRITES,
    )


async def ai_health() -> dict[str, dict[str, str | int]]:
    """Readiness detail for the AI dependencies.

    Reported separately from the application's own readiness: neither Ollama
    nor Pinecone being down should make the service unready, because no AI call
    sits on a write path.
    """
    embeddings = _embeddings()
    store = _store()

    ollama_ok = await embeddings.health()
    pinecone_ok = await store.health()

    out: dict[str, dict[str, str | int]] = {
        "ollama": {"status": "ok" if ollama_ok else "error", "model": settings.OLLAMA_EMBED_MODEL},
        "pinecone": {
            "status": "ok" if pinecone_ok else "error",
            "index": settings.PINECONE_INDEX,
        },
    }
    if ollama_ok:
        try:
            out["ollama"]["dimension"] = await embeddings.warm()
        except Exception as exc:
            out["ollama"] = {"status": "error", "error": type(exc).__name__}
    return out
