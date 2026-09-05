"""Reading tickets out of Postgres, and describing them for the index.

## What "ingestion" means here

In a document-RAG system this package holds connectors and parsers: PDF
readers, HTML strippers, OCR. There are none here, and the reason is worth
being able to state — **the corpus is not a pile of files, it is a table this
application writes itself**.

That removes an entire category of problem. No format detection, no encoding
guesses, no parser that silently returns an empty string for a scanned page, no
"the loader picked up a temp file". The source of truth is a row with typed
columns and a foreign key to the tenant.

It also removes the usual freshness problem. Document pipelines re-crawl on a
schedule and hope. Here, the application knows the moment a ticket changes,
because it is the thing that changed it — so ingestion is event-driven through
the outbox rather than a nightly sweep.

## What is left, and it is not nothing

Two jobs survive:

* **Loading** — fetching the tickets to index, always excluding soft-deleted
  ones. Centralised here so "which tickets belong in the index" has one answer
  rather than one per caller.
* **Describing** — building the metadata stored beside the vector. This is the
  ingestion decision with real consequences, because it fixes what a search can
  later filter on without re-embedding the corpus.

The text assembly is deliberately *not* here — it lives in `chunking/policy.py`,
because how a record becomes embeddable text is a chunking decision even when
the answer is "do not split it".
"""

from __future__ import annotations

import uuid

from sqlalchemy import Select, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.modules.tickets.models import Ticket


def indexable_tickets() -> Select[tuple[Ticket]]:
    """The base query for everything that belongs in the index.

    One definition, so a caller cannot forget the soft-delete filter. A deleted
    ticket that keeps its vector goes on surfacing in similarity results
    forever, and the reconciler would treat it as a legitimate orphan rather
    than as a bug.
    """
    return select(Ticket).where(Ticket.deleted_at.is_(None))


async def load_ticket(session: AsyncSession, ticket_id: uuid.UUID) -> Ticket | None:
    """One ticket, or None if it is gone.

    None rather than raising: a ticket deleted between a job being queued and
    the worker running it is ordinary, and failing the job would retry it five
    times against a row that will never come back.
    """
    result = await session.execute(indexable_tickets().where(Ticket.id == ticket_id))
    return result.scalar_one_or_none()


async def load_tickets(session: AsyncSession, ticket_ids: list[uuid.UUID]) -> list[Ticket]:
    """Several tickets, silently skipping any that no longer exist."""
    if not ticket_ids:
        return []
    result = await session.execute(indexable_tickets().where(Ticket.id.in_(ticket_ids)))
    return list(result.scalars().all())


def build_metadata(ticket: Ticket, source_hash: str) -> dict[str, object]:
    """Metadata stored alongside the vector.

    Chosen for *filtering*, not display — status, team, and client are here so
    a search can be narrowed inside Pinecone before results come back. The
    ticket text is deliberately absent: it stays in Postgres, so sensitive
    content lives in exactly one place.

    Adding a field here only affects tickets embedded afterwards. Filtering on
    something this does not already carry means re-embedding the corpus, which
    is why the list is chosen for what a search might need rather than for what
    is convenient today.
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
