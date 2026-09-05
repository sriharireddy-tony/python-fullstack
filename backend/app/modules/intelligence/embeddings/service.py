"""Building and storing ticket embeddings.

The write path. Given a ticket, produce its embedding text, decide whether it
needs re-embedding, and if so store the vector in Pinecone and record the fact in
Postgres.
"""

from __future__ import annotations

import hashlib
import re
import uuid
from dataclasses import dataclass

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.logging import get_logger
from app.modules.intelligence.chunking.policy import build_embedding_text
from app.modules.intelligence.ingestion.ticket_source import (
    build_metadata,
    load_ticket,
)
from app.modules.intelligence.ports import EmbeddingProvider, VectorStore
from app.modules.intelligence.repository import VectorStateRepository
from app.modules.tickets.models import Ticket

logger = get_logger(__name__)

_WHITESPACE = re.compile(r"\s+")


@dataclass(frozen=True, slots=True)
class EmbeddingInput:
    ticket_id: uuid.UUID
    tenant_id: uuid.UUID
    text: str
    source_hash: str
    metadata: dict[str, object]


def compute_source_hash(text: str, model: str, instruction_version: str) -> str:
    """Identity of an embedding's inputs.

    The model name and instruction version are included on purpose. Change
    either and every stored vector is stale, so folding them in forces a
    re-embed automatically rather than leaving a corpus half in one convention
    and half in another.

    Whitespace is normalised so a trivial edit does not trigger pointless work.
    """
    normalised = _WHITESPACE.sub(" ", text).strip().lower()
    payload = f"{model}|{instruction_version}|{normalised}"
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


class EmbeddingService:
    def __init__(
        self,
        session: AsyncSession,
        embeddings: EmbeddingProvider,
        store: VectorStore,
    ) -> None:
        self._session = session
        self._embeddings = embeddings
        self._store = store
        self._state = VectorStateRepository(session)

    async def prepare(self, ticket: Ticket, *, force: bool = False) -> EmbeddingInput | None:
        """Decide whether a ticket needs embedding, and produce its input.

        Returns None when the stored hash already matches — which is what makes
        the whole pipeline idempotent. A retried job, a duplicate job, or a
        full backfill over an already-embedded corpus each cost one cheap query
        instead of an embed call.

        ``force`` overrides that skip and re-embeds regardless. It exists for
        the cases the hash cannot see: a vector deleted directly in the store,
        a suspected bad write, or an operator who simply wants the work redone.
        It is opt-in because the default has to stay cheap — a console that
        re-embedded everything on every click would make the button expensive
        rather than useful.
        """
        text = build_embedding_text(ticket)
        if not text:
            return None

        instruction_version = getattr(
            self._embeddings, "instruction_version", settings.EMBED_INSTRUCTION_VERSION
        )
        source_hash = compute_source_hash(text, self._embeddings.model_name, instruction_version)

        if not force:
            existing = await self._state.get(ticket.id, self._embeddings.model_name)
            if existing is not None and existing.source_hash == source_hash:
                return None

        return EmbeddingInput(
            ticket_id=ticket.id,
            tenant_id=ticket.tenant_id,
            text=text,
            source_hash=source_hash,
            metadata=build_metadata(ticket, source_hash),
        )

    async def embed_tickets(self, tickets: list[Ticket], *, force: bool = False) -> int:
        """Embed a batch, skipping anything unchanged. Returns the count done."""
        if not tickets:
            return 0

        pending: list[EmbeddingInput] = []
        for ticket in tickets:
            prepared = await self.prepare(ticket, force=force)
            if prepared is not None:
                pending.append(prepared)

        if not pending:
            return 0

        dimension = self._embeddings.dimension
        tenant_id = pending[0].tenant_id
        await self._store.ensure_collection(tenant_id, dimension)

        vectors = await self._embeddings.embed_documents([p.text for p in pending])

        await self._store.upsert_many(
            tenant_id,
            [(p.ticket_id, vector, p.metadata) for p, vector in zip(pending, vectors, strict=True)],
        )

        for prepared in pending:
            await self._state.record(
                tenant_id=prepared.tenant_id,
                ticket_id=prepared.ticket_id,
                model=self._embeddings.model_name,
                dimension=dimension,
                source_hash=prepared.source_hash,
            )

        logger.info(
            "tickets embedded",
            extra={"count": len(pending), "skipped": len(tickets) - len(pending)},
        )
        return len(pending)

    async def embed_ticket_id(self, ticket_id: uuid.UUID, *, force: bool = False) -> bool:
        ticket = await load_ticket(self._session, ticket_id)
        if ticket is None:
            # Deleted between enqueue and execution — remove any stale vector
            # rather than leaving it to surface in future searches.
            return False
        return await self.embed_tickets([ticket], force=force) > 0

    async def remove_ticket(self, tenant_id: uuid.UUID, ticket_id: uuid.UUID) -> None:
        """Delete a vector and forget its bookkeeping.

        A soft-deleted ticket must lose its vector, or it keeps appearing in
        similarity results forever.
        """
        await self._store.delete(tenant_id, [ticket_id])
        await self._state.forget([ticket_id])
