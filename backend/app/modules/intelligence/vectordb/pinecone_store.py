"""Pinecone vector store adapter, one namespace per tenant.

Replaces the Chroma adapter. The port it satisfies is unchanged, so nothing
above this file — no node, no graph, no service — knows the store moved. That
is the return on having defined ``VectorStore`` as a Protocol rather than
importing a client directly.

## Why namespaces rather than an index per tenant

Chroma gave every tenant its own collection because collections are free.
Pinecone indexes are not: they are provisioned, billable resources, and a free
tier caps how many exist. Index-per-tenant would mean provisioning latency on
every new tenant and a hard ceiling on tenant count.

A **namespace** is Pinecone's own multi-tenant primitive and gives the property
that actually matters: a query names exactly one namespace and cannot read
across namespaces. There is no filter to forget, which was the reason
collection-per-tenant was chosen over metadata filtering in the first place.
The mechanism changed; the guarantee did not.

## Two Pinecone behaviours worth knowing

* **Scores are similarities, not distances.** Chroma returns cosine *distance*
  (smaller is closer) and the old adapter converted with ``1 - d``. Pinecone
  returns cosine *similarity* directly (larger is closer), so the score passes
  through untouched. Converting anyway would have inverted every ranking while
  still returning plausible-looking numbers.
* **Index creation is asynchronous.** ``create_index`` returns before the index
  can serve traffic, so the first upsert against a fresh index fails unless
  something waits for ready. ``ensure_collection`` does that waiting.

The SDK is synchronous, so every call is pushed to a thread — otherwise a
search would block the event loop and stall every other request on the worker.
That is the same shape the Chroma adapter used, deliberately: one concurrency
model in the codebase, not two.
"""

from __future__ import annotations

import asyncio
import time
import uuid
from typing import Any

from pinecone import Pinecone, ServerlessSpec

from app.core.config import settings
from app.core.logging import get_logger
from app.modules.intelligence.ports import VectorHit

logger = get_logger(__name__)


def namespace_for(tenant_id: uuid.UUID) -> str:
    """Derive the namespace name. Never accepts client input.

    Derived from the authenticated tenant id, so the caller cannot influence
    which partition is read — the same property the Chroma collection name had.
    """
    return f"tenant_{tenant_id.hex}"


class PineconeVectorStore:
    """Per-tenant namespaces inside one serverless Pinecone index."""

    def __init__(
        self,
        api_key: str | None = None,
        index_name: str | None = None,
    ) -> None:
        key = api_key or settings.PINECONE_API_KEY
        if not key:
            raise RuntimeError(
                "PINECONE_API_KEY is not set. The vector store cannot start without it."
            )
        self._client = Pinecone(api_key=key)
        self._index_name = index_name or settings.PINECONE_INDEX
        self._index: Any | None = None
        self._ready = False

    # ---------------------------------------------------------------- index

    def _ensure_index_sync(self, dimension: int) -> Any:
        """Create the index if absent, then wait until it can serve traffic.

        Runs in a thread. The readiness wait is the important part: Pinecone
        returns from ``create_index`` while the index is still provisioning,
        and an upsert sent in that window fails with a confusing error about
        the index not existing.
        """
        if not self._client.has_index(self._index_name):
            logger.info(
                "creating pinecone index",
                extra={
                    "index": self._index_name,
                    "dimension": dimension,
                    "cloud": settings.PINECONE_CLOUD,
                    "region": settings.PINECONE_REGION,
                },
            )
            self._client.create_index(
                name=self._index_name,
                dimension=dimension,
                # Cosine compares direction. Magnitude mostly tracks text
                # length, so a euclidean metric would make a long and a short
                # ticket about the same bug look dissimilar. Fixed at creation.
                metric="cosine",
                spec=ServerlessSpec(
                    cloud=settings.PINECONE_CLOUD,
                    region=settings.PINECONE_REGION,
                ),
            )

        deadline = time.monotonic() + settings.PINECONE_READY_TIMEOUT
        while time.monotonic() < deadline:
            description = self._client.describe_index(self._index_name)
            if description.status.get("ready"):
                # `dimension` is Optional in the SDK's model because an index
                # created for integrated embedding declares no dimension of its
                # own. Ours always does, so a missing value means something is
                # wrong with the index rather than with this check.
                if description.dimension is None:
                    raise RuntimeError(
                        f"Pinecone index {self._index_name!r} reports no dimension. "
                        f"It was likely created for integrated embedding; this system "
                        f"supplies its own vectors and needs a dimensioned index."
                    )
                existing = int(description.dimension)
                if existing != dimension:
                    # Loud, because the alternative is every upsert failing one
                    # by one with a dimension error and no statement of why.
                    raise RuntimeError(
                        f"Pinecone index {self._index_name!r} has dimension {existing}, "
                        f"but the embedding model emits {dimension}. Delete the index or "
                        f"point PINECONE_INDEX at a new name."
                    )
                return self._client.Index(self._index_name)
            time.sleep(1.0)

        raise RuntimeError(
            f"Pinecone index {self._index_name!r} was not ready within "
            f"{settings.PINECONE_READY_TIMEOUT}s."
        )

    async def ensure_collection(self, tenant_id: uuid.UUID, dimension: int) -> None:
        """Ensure the index exists and is ready.

        Namespaces need no creation step — Pinecone makes one on first write —
        so this is about the index. ``tenant_id`` is in the signature because
        the port requires it, and keeping it means the store can move back to a
        per-tenant resource later without changing any caller.
        """
        if self._ready:
            return
        self._index = await asyncio.to_thread(self._ensure_index_sync, dimension)
        self._ready = True
        logger.info(
            "pinecone index ready",
            extra={"index": self._index_name, "dimension": dimension},
        )

    def _require_index(self) -> Any:
        if self._index is None:
            self._index = self._client.Index(self._index_name)
        return self._index

    # --------------------------------------------------------------- writes

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

        namespace = namespace_for(tenant_id)
        vectors = [
            {
                "id": str(ticket_id),
                "values": vector,
                "metadata": _clean_metadata(metadata),
            }
            for ticket_id, vector, metadata in items
        ]

        def _write() -> None:
            # upsert, not insert: a re-embed after a ticket edit must replace
            # cleanly rather than raise or duplicate.
            self._require_index().upsert(vectors=vectors, namespace=namespace)

        await asyncio.to_thread(_write)

    async def delete(self, tenant_id: uuid.UUID, ticket_ids: list[uuid.UUID]) -> None:
        if not ticket_ids:
            return

        namespace = namespace_for(tenant_id)
        ids = [str(t) for t in ticket_ids]

        def _delete() -> None:
            try:
                self._require_index().delete(ids=ids, namespace=namespace)
            except Exception as exc:
                # Deleting from a namespace that never existed is a 404 on some
                # Pinecone versions. The desired end state -- the vector is not
                # there -- already holds, so this is not a failure.
                if "not found" not in str(exc).lower():
                    raise

        await asyncio.to_thread(_delete)

    # ---------------------------------------------------------------- reads

    async def search(
        self,
        tenant_id: uuid.UUID,
        embedding: list[float],
        limit: int,
        exclude: set[uuid.UUID] | None = None,
        where: dict[str, Any] | None = None,
    ) -> list[VectorHit]:
        exclude = exclude or set()
        namespace = namespace_for(tenant_id)
        # Over-fetch, because the excluded ids (usually the ticket itself) are
        # removed after the search and would otherwise eat into the limit.
        fetch = limit + len(exclude) + 5

        def _query() -> Any:
            return self._require_index().query(
                vector=embedding,
                top_k=fetch,
                namespace=namespace,
                include_metadata=True,
                filter=where or None,
            )

        try:
            raw = await asyncio.to_thread(_query)
        except Exception:
            # An empty namespace, or one that does not exist yet, is ordinary
            # before the first embed. Returning no hits keeps the lexical half
            # of the hybrid working instead of failing the whole request.
            logger.warning("pinecone query failed", extra={"namespace": namespace})
            return []

        hits: list[VectorHit] = []
        for match in raw.get("matches", []) or []:
            try:
                ticket_id = uuid.UUID(str(match["id"]))
            except (ValueError, KeyError):
                continue
            if ticket_id in exclude:
                continue
            hits.append(
                VectorHit(
                    ticket_id=ticket_id,
                    # Pinecone returns cosine SIMILARITY (larger is closer),
                    # unlike Chroma's distance. No conversion -- inverting this
                    # would reverse every ranking while still looking sane.
                    score=float(match.get("score", 0.0)),
                    metadata=dict(match.get("metadata") or {}),
                )
            )
            if len(hits) >= limit:
                break
        return hits

    async def count(self, tenant_id: uuid.UUID) -> int:
        """How many vectors this tenant has.

        Counted by listing ids, **not** by ``describe_index_stats``, and that is
        the opposite of the obvious implementation. Serverless index statistics
        are eventually consistent on a timescale of minutes, not seconds: a
        namespace holding three freshly-upserted vectors reports
        ``total_vector_count=0`` and ``namespaces={}`` while ``list()`` returns
        all three ids immediately.

        Using stats here made the console report an empty index straight after a
        successful embed, which reads as "the button did nothing" -- the single
        worst thing an operations page can say when the work in fact succeeded.

        The cost is real and worth naming: listing pages every id, so this is
        O(vectors) rather than O(1). At console scale that is the right trade,
        because a wrong number is worse than a slow one. A corpus large enough
        for the listing to hurt should show an approximate count from stats and
        say that it is approximate.
        """
        return len(await self.list_ids(tenant_id))

    async def list_ids(self, tenant_id: uuid.UUID) -> set[uuid.UUID]:
        """Every id in the tenant's namespace. Used by reconciliation.

        Errors deliberately propagate. This feeds drift detection, and a
        reconciliation that silently reports an empty store would claim every
        vector is missing and re-embed the entire corpus. Failing loudly is the
        cheaper outcome.

        The iteration shape is worth stating: ``list()`` yields ``ListResponse``
        pages, not id strings, and each page carries ``.vectors`` of items with
        an ``.id``. Iterating the page directly yields nothing useful and -- with
        a broad ``except`` around it -- looks exactly like an empty namespace.
        """
        namespace = namespace_for(tenant_id)

        def _all() -> set[uuid.UUID]:
            out: set[uuid.UUID] = set()
            for page in self._require_index().list(namespace=namespace):
                for item in page.vectors or []:
                    try:
                        out.add(uuid.UUID(str(item.id)))
                    except ValueError:
                        # An id that is not a ticket uuid is not ours to report.
                        continue
            return out

        return await asyncio.to_thread(_all)

    async def health(self) -> bool:
        """Is Pinecone reachable?

        Deliberately *not* ``has_index``. The index is created lazily on first
        embed, so asking about the index reports "unhealthy" for a correctly
        configured service that simply has not embedded anything yet -- which is
        precisely the state this system starts in, with automatic embedding off.
        Listing indexes answers the question actually being asked: can we talk
        to Pinecone with these credentials.
        """
        try:
            await asyncio.to_thread(lambda: list(self._client.list_indexes()))
        except Exception:
            return False
        return True


def _clean_metadata(metadata: dict[str, Any]) -> dict[str, Any]:
    """Pinecone accepts str, number, bool, and list-of-str in metadata.

    Nulls are dropped rather than stringified — a metadata value of "None"
    would silently match a filter looking for the string.
    """
    out: dict[str, Any] = {}
    for key, value in metadata.items():
        if value is None:
            continue
        if isinstance(value, str | int | float | bool) or (
            isinstance(value, list) and all(isinstance(v, str) for v in value)
        ):
            out[key] = value
        else:
            out[key] = str(value)
    return out
