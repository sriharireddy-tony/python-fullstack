"""Chroma vector store adapter, one collection per tenant.

Uses the ``chromadb`` HTTP client directly rather than ``langchain_chroma``.
That wrapper is built around a single collection that owns its own embedding
function; we need many collections and we supply precomputed vectors, so
fighting the abstraction would cost more than it returns. The LangChain surface
lives where it fits naturally — the graphs.

**Tenant isolation is structural.** The collection name derives from the
authenticated tenant id, so opening the wrong collection returns nothing rather
than another tenant's data. Chroma has no row-level security, so a shared
collection with a metadata filter would make one forgotten ``where`` clause a
silent leak.
"""

from __future__ import annotations

import asyncio
import uuid
from typing import Any

import chromadb
from chromadb.config import Settings as ChromaSettings

from app.core.config import settings
from app.core.logging import get_logger
from app.modules.intelligence.ports import VectorHit

logger = get_logger(__name__)


def collection_name(tenant_id: uuid.UUID) -> str:
    """Derive the collection name. Never accepts client input."""
    return f"tickets_{tenant_id.hex}"


class ChromaVectorStore:
    """Per-tenant collections over a Chroma HTTP server.

    The chromadb client is synchronous, so every call is pushed to a thread —
    otherwise a search would block the event loop and stall every other
    request on the worker.
    """

    def __init__(self, host: str | None = None, port: int | None = None) -> None:
        self._client = chromadb.HttpClient(
            host=host or settings.CHROMA_HOST,
            port=port or settings.CHROMA_PORT,
            settings=ChromaSettings(anonymized_telemetry=False),
        )
        self._known: set[str] = set()

    # ------------------------------------------------------------ collections

    def _collection(self, tenant_id: uuid.UUID, dimension: int | None = None) -> Any:
        name = collection_name(tenant_id)
        return self._client.get_or_create_collection(
            name=name,
            metadata={
                # Chroma defaults to L2. For text embeddings cosine is correct:
                # it compares direction, and magnitude mostly tracks length, so
                # L2 would make a long and a short ticket about the same bug
                # look dissimilar. Not changeable after creation.
                "hnsw:space": "cosine",
                **({"dimension": dimension} if dimension else {}),
            },
        )

    async def ensure_collection(self, tenant_id: uuid.UUID, dimension: int) -> None:
        name = collection_name(tenant_id)
        if name in self._known:
            return
        await asyncio.to_thread(self._collection, tenant_id, dimension)
        self._known.add(name)
        logger.info(
            "chroma collection ready",
            extra={"collection": name, "dimension": dimension},
        )

    # ----------------------------------------------------------------- writes

    async def upsert(
        self,
        tenant_id: uuid.UUID,
        ticket_id: uuid.UUID,
        embedding: list[float],
        metadata: dict[str, Any],
    ) -> None:
        await self.upsert_many(tenant_id, [(ticket_id, embedding, metadata)])

    async def upsert_many(
        self,
        tenant_id: uuid.UUID,
        items: list[tuple[uuid.UUID, list[float], dict[str, Any]]],
    ) -> None:
        if not items:
            return

        def _write() -> None:
            collection = self._collection(tenant_id)
            # upsert, not add: a re-embed after a ticket edit must replace
            # cleanly rather than raise or duplicate.
            collection.upsert(
                ids=[str(tid) for tid, _, _ in items],
                embeddings=[vec for _, vec, _ in items],
                metadatas=[_clean_metadata(meta) for _, _, meta in items],
            )

        await asyncio.to_thread(_write)

    async def delete(self, tenant_id: uuid.UUID, ticket_ids: list[uuid.UUID]) -> None:
        if not ticket_ids:
            return

        def _delete() -> None:
            self._collection(tenant_id).delete(ids=[str(t) for t in ticket_ids])

        await asyncio.to_thread(_delete)

    # ------------------------------------------------------------------ reads

    async def search(
        self,
        tenant_id: uuid.UUID,
        embedding: list[float],
        limit: int,
        exclude: set[uuid.UUID] | None = None,
        where: dict[str, Any] | None = None,
    ) -> list[VectorHit]:
        exclude = exclude or set()
        # Over-fetch, because the excluded ids (usually the ticket itself) are
        # removed after the search and would otherwise eat into the limit.
        fetch = limit + len(exclude) + 5

        def _query() -> Any:
            collection = self._collection(tenant_id)
            if collection.count() == 0:
                return None
            return collection.query(
                query_embeddings=[embedding],
                n_results=min(fetch, max(collection.count(), 1)),
                where=where or None,
                include=["distances", "metadatas"],
            )

        raw = await asyncio.to_thread(_query)
        if raw is None:
            return []

        ids = raw.get("ids", [[]])[0]
        distances = raw.get("distances", [[]])[0]
        metadatas = raw.get("metadatas", [[]])[0] or [{}] * len(ids)

        hits: list[VectorHit] = []
        for raw_id, distance, metadata in zip(ids, distances, metadatas, strict=False):
            try:
                ticket_id = uuid.UUID(raw_id)
            except ValueError:
                continue
            if ticket_id in exclude:
                continue
            hits.append(
                VectorHit(
                    ticket_id=ticket_id,
                    # Chroma returns cosine DISTANCE (smaller is closer).
                    # Converted once, here, so no caller downstream has to
                    # remember the direction.
                    score=1.0 - float(distance),
                    metadata=dict(metadata or {}),
                )
            )
            if len(hits) >= limit:
                break
        return hits

    async def count(self, tenant_id: uuid.UUID) -> int:
        return await asyncio.to_thread(lambda: self._collection(tenant_id).count())

    async def list_ids(self, tenant_id: uuid.UUID) -> set[uuid.UUID]:
        """Every id in the collection. Used only by reconciliation."""

        def _all() -> set[uuid.UUID]:
            collection = self._collection(tenant_id)
            out: set[uuid.UUID] = set()
            offset, page = 0, 1000
            while True:
                batch = collection.get(limit=page, offset=offset, include=[])
                found = batch.get("ids") or []
                for raw_id in found:
                    try:
                        out.add(uuid.UUID(raw_id))
                    except ValueError:
                        continue
                if len(found) < page:
                    return out
                offset += page

        return await asyncio.to_thread(_all)

    async def health(self) -> bool:
        try:
            return await asyncio.to_thread(lambda: bool(self._client.heartbeat()))
        except Exception:
            return False


def _clean_metadata(metadata: dict[str, Any]) -> dict[str, Any]:
    """Chroma accepts only str, int, float and bool in metadata.

    Nulls and anything else are dropped rather than stringified — a metadata
    value of "None" would silently match a filter looking for the string.
    """
    out: dict[str, Any] = {}
    for key, value in metadata.items():
        if value is None:
            continue
        out[key] = value if isinstance(value, str | int | float | bool) else str(value)
    return out
