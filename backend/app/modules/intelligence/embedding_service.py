"""Building and storing ticket embeddings.

The write path. Given a ticket, produce its embedding text, decide whether it
needs re-embedding, and if so store the vector in Chroma and record the fact in
Postgres.
"""

from __future__ import annotations

import hashlib
import re
import uuid
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.logging import get_logger
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


def build_embedding_text(ticket: Ticket) -> str:
    """The text that represents a ticket for similarity purposes.

    Bounded, because the model truncates at its token limit anyway — and an
    unbounded field means the first N tokens silently decide the vector with no
    indication of what was cut. Title first, so if truncation happens the title
    is what survives.

    Deliberately excluded:

    * **comments** — they change constantly, so including them means
      re-embedding forever and the vector never settles.
    * **resolution notes** — written *after* the bug is understood. Including
      them leaks the outcome into a signal used at triage time, which inflates
      offline scores and produces a system that performs worse in reality.
    * **client name** — would cluster tickets by customer rather than by
      problem. "Acme payroll bug" and "Acme leave bug" share "Acme". The client
      is a metadata filter, not part of the meaning.
    """
    parts = [
        (ticket.title or "")[: settings.EMBED_TITLE_MAX],
        (ticket.description or "")[: settings.EMBED_DESCRIPTION_MAX],
        (ticket.steps_to_reproduce or "")[: settings.EMBED_STEPS_MAX],
    ]
    return "\n\n".join(part.strip() for part in parts if part and part.strip())


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


def build_metadata(ticket: Ticket, source_hash: str) -> dict[str, object]:
    """Metadata stored alongside the vector.

    Chosen for *filtering*, not display — status, team, and client are here so
    a search can be narrowed inside Chroma before results come back. The ticket
    text is deliberately absent: it stays in Postgres, so sensitive content
    lives in exactly one place.
    """
    return {
        "ticket_number": ticket.ticket_number,
        "team_id": str(ticket.team_id),
        "client_id": str(ticket.client_id),
        "status": ticket.status.value,
        "priority": ticket.priority.value,
        "created_at": ticket.created_at.isoformat(),
        "source_hash": source_hash,
    }


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

    async def prepare(self, ticket: Ticket) -> EmbeddingInput | None:
        """Decide whether a ticket needs embedding, and produce its input.

        Returns None when the stored hash already matches — which is what makes
        the whole pipeline idempotent. A retried job, a duplicate job, or a
        full backfill over an already-embedded corpus each cost one cheap query
        instead of an embed call.
        """
        text = build_embedding_text(ticket)
        if not text:
            return None

        instruction_version = getattr(
            self._embeddings, "instruction_version", settings.EMBED_INSTRUCTION_VERSION
        )
        source_hash = compute_source_hash(text, self._embeddings.model_name, instruction_version)

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

    async def embed_tickets(self, tickets: list[Ticket]) -> int:
        """Embed a batch, skipping anything unchanged. Returns the count done."""
        if not tickets:
            return 0

        pending: list[EmbeddingInput] = []
        for ticket in tickets:
            prepared = await self.prepare(ticket)
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

    async def embed_ticket_id(self, ticket_id: uuid.UUID) -> bool:
        result = await self._session.execute(
            select(Ticket).where(Ticket.id == ticket_id, Ticket.deleted_at.is_(None))
        )
        ticket = result.scalar_one_or_none()
        if ticket is None:
            # Deleted between enqueue and execution — remove any stale vector
            # rather than leaving it to surface in future searches.
            return False
        return await self.embed_tickets([ticket]) > 0

    async def remove_ticket(self, tenant_id: uuid.UUID, ticket_id: uuid.UUID) -> None:
        """Delete a vector and forget its bookkeeping.

        A soft-deleted ticket must lose its vector, or it keeps appearing in
        similarity results forever.
        """
        await self._store.delete(tenant_id, [ticket_id])
        await self._state.forget([ticket_id])
