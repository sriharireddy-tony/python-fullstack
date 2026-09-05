"""The embedding console: what is indexed, and the operations to change it.

Backs the operator page. Three questions and three verbs:

* **What is embedded?** Every ticket, joined to its vector bookkeeping, with a
  state that distinguishes "never embedded" from "embedded but the text has
  changed since".
* **Embed these.** Selected tickets, or everything matching a filter.
* **Delete these.** Remove vectors without touching the tickets.

## Why the work happens in the request

Embedding here runs synchronously and returns what it did, rather than queueing
jobs. That is a deliberate inversion of how the automatic path works, and the
reason is the audience: a person is watching. A queued job means the page shows
"submitted" and the operator has to go and find out whether it worked, which is
the opposite of what a console is for.

The trade is bounded rather than ignored: ``MAX_SYNC_BATCH`` caps how much one
request may do, and anything larger is refused with a message rather than
silently truncated. Bulk corpus migration -- re-embedding after a model change,
say -- belongs on the queue, and `scripts/ai_backfill.py` is that path.

## What this module deliberately cannot do

There is no verb here that edits a ticket. The console owns the *index*, not
the content: it can rebuild a vector, and it can delete one, but the text it
embeds always comes from the tickets table. That keeps a single source of
truth, and it means no operator action can make the index disagree with the
record it describes.
"""

from __future__ import annotations

import time
import uuid
from dataclasses import dataclass
from enum import StrEnum
from typing import Any

from sqlalchemy import Select, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.logging import get_logger
from app.modules.intelligence.chunking.policy import build_embedding_text
from app.modules.intelligence.embeddings.service import (
    EmbeddingService,
    compute_source_hash,
)
from app.modules.intelligence.models import TicketVectorState
from app.modules.intelligence.ports import EmbeddingProvider, VectorStore
from app.modules.tickets.models import Ticket

logger = get_logger(__name__)

#: The most tickets one console request may embed.
#:
#: Chosen so the request stays inside a normal HTTP timeout on a local model.
#: Refusing beyond it is better than truncating, because an operator who asked
#: for 500 and silently got 100 will believe the other 400 are done.
MAX_SYNC_BATCH = 100


class EmbeddingState(StrEnum):
    """Where one ticket stands relative to the index."""

    #: No vector has ever been written for this ticket with the current model.
    NOT_EMBEDDED = "not_embedded"
    #: Embedded, and the text still hashes to what was embedded.
    EMBEDDED = "embedded"
    #: Embedded, but the ticket's text has changed since. The vector is a
    #: description of a ticket that no longer exists in that form -- which is
    #: worse than no vector, because it retrieves confidently and wrongly.
    STALE = "stale"


@dataclass(frozen=True, slots=True)
class EmbeddingRow:
    """One ticket's indexing status, as the console shows it."""

    ticket_id: uuid.UUID
    ticket_number: int
    title: str
    status: str
    priority: str
    team_id: uuid.UUID
    state: EmbeddingState
    model: str | None
    dimension: int | None
    embedded_at: Any | None
    #: Characters of text that would be embedded. Surfaced because an operator
    #: debugging a bad match wants to know how much of the ticket the model
    #: actually saw.
    text_length: int


@dataclass(frozen=True, slots=True)
class EmbeddingSummary:
    total: int
    embedded: int
    stale: int
    not_embedded: int
    #: What the vector store itself reports, which is the number that matters
    #: when the two disagree.
    vectors_in_store: int
    model: str
    dimension: int | None


@dataclass(frozen=True, slots=True)
class EmbedOutcome:
    requested: int
    embedded: int
    skipped: int
    failed: int
    duration_seconds: float
    errors: list[str]


class EmbeddingConsole:
    """Read and operate the tenant's slice of the vector index.

    The session must already be tenant-scoped: every query below relies on
    row-level security for isolation rather than on a `tenant_id` filter that
    could be forgotten.
    """

    def __init__(
        self,
        session: AsyncSession,
        tenant_id: uuid.UUID,
        embeddings: EmbeddingProvider,
        store: VectorStore,
    ) -> None:
        self._session = session
        self._tenant_id = tenant_id
        self._embeddings = embeddings
        self._store = store

    # ------------------------------------------------------------------ read

    def _base_query(
        self,
        *,
        team_id: uuid.UUID | None,
        status: str | None,
        search: str | None,
    ) -> Select[tuple[Ticket, TicketVectorState]]:
        """Tickets left-joined to their bookkeeping row.

        A LEFT join rather than two queries: "which tickets have no vector" is
        the console's primary question, and expressing it as an absent join row
        keeps it one indexed query instead of loading every id into Python.

        The return type says ``TicketVectorState`` but the second element is
        ``None`` for any ticket that has never been embedded -- SQLAlchemy's
        stubs cannot express that an outer join makes its right side optional.
        Callers must treat it as nullable; ``list_rows`` does.
        """
        query = (
            select(Ticket, TicketVectorState)
            .outerjoin(
                TicketVectorState,
                (TicketVectorState.ticket_id == Ticket.id)
                & (TicketVectorState.model == self._embeddings.model_name),
            )
            .where(Ticket.deleted_at.is_(None))
        )
        if team_id is not None:
            query = query.where(Ticket.team_id == team_id)
        if status:
            query = query.where(Ticket.status == status)
        if search:
            # Reuses the generated search_vector the ticket list already has.
            query = query.where(
                Ticket.search_vector.op("@@")(func.plainto_tsquery("english", search))
            )
        return query

    async def list_rows(
        self,
        *,
        team_id: uuid.UUID | None = None,
        status: str | None = None,
        state: EmbeddingState | None = None,
        search: str | None = None,
        limit: int = 100,
        offset: int = 0,
    ) -> tuple[list[EmbeddingRow], int]:
        """One page of ticket indexing status, plus the unpaged total.

        ``state`` is filtered in Python rather than SQL because deciding
        between EMBEDDED and STALE means recomputing the source hash, which
        needs the ticket text and the model's instruction version. Pushing that
        into SQL would mean duplicating the hashing rule in two languages, and
        the two would drift.
        """
        query = self._base_query(team_id=team_id, status=status, search=search)

        total = await self._session.scalar(select(func.count()).select_from(query.subquery()))

        rows_result = await self._session.execute(
            query.order_by(Ticket.ticket_number.desc()).limit(limit).offset(offset)
        )

        instruction_version = getattr(
            self._embeddings, "instruction_version", settings.EMBED_INSTRUCTION_VERSION
        )

        rows: list[EmbeddingRow] = []
        for ticket, vector_state in rows_result.all():
            text = build_embedding_text(ticket)
            current_hash = compute_source_hash(
                text, self._embeddings.model_name, instruction_version
            )

            if vector_state is None:
                ticket_state = EmbeddingState.NOT_EMBEDDED
            elif vector_state.source_hash == current_hash:
                ticket_state = EmbeddingState.EMBEDDED
            else:
                ticket_state = EmbeddingState.STALE

            if state is not None and ticket_state is not state:
                continue

            rows.append(
                EmbeddingRow(
                    ticket_id=ticket.id,
                    ticket_number=ticket.ticket_number,
                    title=ticket.title,
                    status=ticket.status.value,
                    priority=ticket.priority.value,
                    team_id=ticket.team_id,
                    state=ticket_state,
                    model=vector_state.model if vector_state else None,
                    dimension=vector_state.dimension if vector_state else None,
                    embedded_at=vector_state.synced_at if vector_state else None,
                    text_length=len(text),
                )
            )

        return rows, int(total or 0)

    async def summary(self) -> EmbeddingSummary:
        """Counts across the whole tenant, plus what the store itself holds.

        The store count is queried rather than inferred. Postgres knows what it
        *recorded*; only Pinecone knows what it *has*, and the gap between them
        is the drift this console exists to make visible.
        """
        rows, total = await self.list_rows(limit=100_000)
        embedded = sum(1 for r in rows if r.state is EmbeddingState.EMBEDDED)
        stale = sum(1 for r in rows if r.state is EmbeddingState.STALE)

        try:
            in_store = await self._store.count(self._tenant_id)
        except Exception:
            # The console must render even when the vector store is
            # unreachable -- reporting "unknown" is more useful than a 500 on
            # the page an operator opens to find out what is wrong.
            in_store = -1

        dimension: int | None
        try:
            dimension = self._embeddings.dimension
        except RuntimeError:
            dimension = None

        return EmbeddingSummary(
            total=total,
            embedded=embedded,
            stale=stale,
            not_embedded=total - embedded - stale,
            vectors_in_store=in_store,
            model=self._embeddings.model_name,
            dimension=dimension,
        )

    # ----------------------------------------------------------------- write

    async def _load(self, ticket_ids: list[uuid.UUID]) -> list[Ticket]:
        result = await self._session.execute(
            select(Ticket).where(Ticket.id.in_(ticket_ids), Ticket.deleted_at.is_(None))
        )
        return list(result.scalars().all())

    async def embed(
        self,
        ticket_ids: list[uuid.UUID],
        *,
        force: bool = False,
    ) -> EmbedOutcome:
        """Embed the named tickets now, and report what actually happened.

        ``force`` re-embeds tickets whose text is unchanged. Without it, asking
        to embed an already-current ticket is a no-op that costs one indexed
        query -- which is why the console can offer "embed" and "re-embed" as
        the same button with a checkbox rather than two code paths.
        """
        if not ticket_ids:
            return EmbedOutcome(0, 0, 0, 0, 0.0, [])
        if len(ticket_ids) > MAX_SYNC_BATCH:
            raise ValueError(
                f"{len(ticket_ids)} tickets requested; this endpoint embeds at most "
                f"{MAX_SYNC_BATCH} at a time. Use scripts/ai_backfill.py for a full corpus."
            )

        started = time.monotonic()
        tickets = await self._load(ticket_ids)
        service = EmbeddingService(self._session, self._embeddings, self._store)

        await self._store.ensure_collection(self._tenant_id, self._embeddings.dimension)

        embedded = 0
        errors: list[str] = []
        # One at a time rather than one batch: a batch that fails loses every
        # ticket in it, and an operator watching a progress count deserves to
        # know which ticket broke rather than that "the batch" broke.
        for ticket in tickets:
            try:
                if await service.embed_tickets([ticket], force=force):
                    embedded += 1
            except Exception as exc:
                errors.append(f"OS-{ticket.ticket_number}: {type(exc).__name__}: {exc}")

        duration = time.monotonic() - started
        logger.info(
            "console embed complete",
            extra={
                "requested": len(ticket_ids),
                "embedded": embedded,
                "failed": len(errors),
                "force": force,
                "seconds": round(duration, 2),
            },
        )
        return EmbedOutcome(
            requested=len(ticket_ids),
            embedded=embedded,
            skipped=len(tickets) - embedded - len(errors),
            failed=len(errors),
            duration_seconds=round(duration, 2),
            errors=errors[:20],
        )

    async def delete(self, ticket_ids: list[uuid.UUID]) -> int:
        """Remove vectors for the named tickets. The tickets are untouched.

        Deletes the bookkeeping row as well as the vector. Leaving the row
        would make the ticket read as EMBEDDED on a console whose whole job is
        to tell the truth about what is indexed.
        """
        if not ticket_ids:
            return 0

        service = EmbeddingService(self._session, self._embeddings, self._store)
        for ticket_id in ticket_ids:
            await service.remove_ticket(self._tenant_id, ticket_id)

        logger.info("console delete complete", extra={"count": len(ticket_ids)})
        return len(ticket_ids)
