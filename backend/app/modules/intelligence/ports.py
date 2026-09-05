"""Interfaces for everything the AI layer depends on from outside.

**No third-party imports in this file, ever.** These Protocols are what let the
node and graph layers stay ignorant of Ollama, Pinecone, Gemini, and LangChain —
so swapping any of them is one adapter file plus a config line.

Enforced mechanically: a CI check greps the node and domain layers for vendor
packages. A rule that verifies itself does not decay the way a convention does.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable


@runtime_checkable
class EmbeddingProvider(Protocol):
    """Turns text into vectors.

    Two methods rather than one, deliberately. Qwen3-Embedding is asymmetric:
    queries carry a task-instruction prefix, documents do not. Getting that
    wrong causes no error, just quietly worse recall — so there is no single
    ``embed()`` that could be called with the wrong intent.
    """

    @property
    def dimension(self) -> int:
        """Vector width. Probed from the model, never configured."""
        ...

    @property
    def model_name(self) -> str:
        """Identity of the model, for ``source_hash`` and bookkeeping."""
        ...

    async def embed_query(self, text: str) -> list[float]: ...

    async def embed_documents(self, texts: list[str]) -> list[list[float]]: ...

    async def health(self) -> bool:
        """Reachability, for the readiness endpoint. Never raises."""
        ...


@dataclass(slots=True)
class VectorHit:
    """One nearest-neighbour result."""

    ticket_id: uuid.UUID
    #: Similarity, where **larger is closer**. Adapters convert from whatever
    #: their backend returns, so no caller has to remember the direction.
    score: float
    metadata: dict[str, Any] = field(default_factory=dict)


@runtime_checkable
class VectorStore(Protocol):
    """Per-tenant vector storage and search.

    Every method takes ``tenant_id`` first and there is no method that omits
    it. Tenant isolation is therefore a property of the signature, not of
    remembering a filter.
    """

    async def ensure_collection(self, tenant_id: uuid.UUID, dimension: int) -> None: ...

    async def upsert(
        self,
        tenant_id: uuid.UUID,
        ticket_id: uuid.UUID,
        embedding: list[float],
        metadata: dict[str, Any],
    ) -> None: ...

    async def upsert_many(
        self,
        tenant_id: uuid.UUID,
        items: list[tuple[uuid.UUID, list[float], dict[str, Any]]],
    ) -> None: ...

    async def search(
        self,
        tenant_id: uuid.UUID,
        embedding: list[float],
        limit: int,
        exclude: set[uuid.UUID] | None = None,
        where: dict[str, Any] | None = None,
    ) -> list[VectorHit]: ...

    async def delete(self, tenant_id: uuid.UUID, ticket_ids: list[uuid.UUID]) -> None: ...

    async def count(self, tenant_id: uuid.UUID) -> int: ...

    async def list_ids(self, tenant_id: uuid.UUID) -> set[uuid.UUID]: ...

    async def health(self) -> bool: ...


@dataclass(slots=True)
class LexicalHit:
    """One full-text search result. Larger score is a better match."""

    ticket_id: uuid.UUID
    score: float


@runtime_checkable
class LexicalSearch(Protocol):
    """Keyword retrieval over the tickets a tenant can see.

    A port even though the implementation is our own Postgres, for two
    reasons: the node layer stays free of SQLAlchemy and testable with a fake,
    and the day this becomes OpenSearch or BM25-in-Rust it is one file.

    No ``tenant_id`` argument, unlike ``VectorStore`` — the implementation is
    bound to a tenant-scoped database session, so row-level security enforces
    isolation and there is no filter to forget. The asymmetry is deliberate:
    each store is isolated by whatever mechanism that store actually has.
    """

    async def search(
        self,
        query_text: str,
        limit: int,
        exclude: set[uuid.UUID] | None = None,
    ) -> list[LexicalHit]:
        """Rank tickets by keyword overlap with ``query_text``.

        Takes text, not a pre-built query. Tokenizing is the implementation's
        job precisely because it must match how the index was built.
        """
        ...

    async def search_exact(self, term: str, limit: int) -> list[LexicalHit]:
        """Tickets containing **every** token of ``term``.

        AND semantics, unlike ``search``. Used to probe a literal identifier,
        where a partial match is not a weaker answer but a wrong one: matching
        ``err`` out of ``ERR-40001`` returns every error report in the corpus.
        """
        ...

    async def by_numbers(self, numbers: tuple[int, ...]) -> dict[int, uuid.UUID]:
        """Resolve ``OS-1234`` references to ticket ids, dropping unknowns."""
        ...


@runtime_checkable
class TicketCatalogue(Protocol):
    """Reads the ticket text the reranker needs to compare.

    A separate port from ``LexicalSearch`` because it answers a different
    question: that one finds ids, this one fetches the text behind ids. Keeping
    them apart means the reranking nodes cannot accidentally issue a search,
    and a fake for one does not have to implement the other.

    Like ``LexicalSearch``, bound to a tenant-scoped session, so row-level
    security is the isolation mechanism and there is no tenant argument.
    """

    async def fetch(self, ticket_ids: list[uuid.UUID]) -> dict[uuid.UUID, dict[str, Any]]:
        """Return ``{ticket_id: {reference, title, description, status, closed_at}}``.

        Missing ids are simply absent from the result -- a ticket deleted since
        retrieval ran is ordinary, and callers skip what is not there.
        """
        ...


@runtime_checkable
class ChatModel(Protocol):
    """A text-in, text-out model.

    Narrow on purpose. The graphs need exactly two things: generate text, and
    generate a validated object. Anything richer would leak the provider's
    surface into the layers above.
    """

    @property
    def model_name(self) -> str: ...

    async def generate(self, system: str, messages: list[dict[str, str]]) -> str: ...

    async def generate_structured(
        self,
        system: str,
        messages: list[dict[str, str]],
        schema: type,
    ) -> Any:
        """Return an instance of ``schema``.

        Structured output is a security control as much as a convenience: the
        model returns a fixed shape and cannot emit an action, whatever a
        malicious ticket description asks it to do.
        """
        ...
