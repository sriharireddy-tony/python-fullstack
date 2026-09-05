"""Wires the intelligence module onto the tickets module's events.

Registered once at startup from `app.main`. This is the only coupling between
the two modules, and it points the correct way: intelligence knows about
tickets, tickets knows nothing about intelligence.
"""

from __future__ import annotations

from app.core.config import settings
from app.core.events import DomainEvent, EventContext, subscribe
from app.core.logging import get_logger
from app.modules.intelligence.models import AiJobKind
from app.modules.intelligence.repository import AiJobRepository

logger = get_logger(__name__)


async def _enqueue_embed(ctx: EventContext) -> None:
    """Queue an embedding job in the emitter's transaction.

    Enqueued unconditionally on every content change — `source_hash` decides
    whether work is actually needed, so there is no change detection to get
    wrong here.

    ## Why this can be switched off

    `AI_AUTO_EMBED_ON_WRITE` gates the enqueue, not the machinery. With it off
    nothing is embedded until someone asks from the embedding console, which is
    what makes the pipeline observable while it is being learned: a ticket sits
    visibly un-embedded until a human presses the button.

    It is off in development for that reason and should be **on** in
    production. Embedding on write is the correct default -- a ticket raised at
    3am is searchable at 3am, not whenever an operator next remembers. The
    console exists for backfill, repair, and model migration, which are the
    jobs a human genuinely has to decide about.
    """
    if not (settings.AI_ENABLED and settings.AI_SIMILARITY_ENABLED):
        return
    if not settings.AI_AUTO_EMBED_ON_WRITE:
        return
    await AiJobRepository(ctx.session).enqueue(
        tenant_id=ctx.tenant_id,
        kind=AiJobKind.EMBED_TICKET,
        payload={"ticket_id": str(ctx.entity_id)},
    )


async def _enqueue_delete(ctx: EventContext) -> None:
    """Remove the vector when a ticket is deleted.

    Without this a soft-deleted ticket keeps surfacing in similarity results
    forever, because Pinecone has no idea it is gone.
    """
    if not settings.AI_ENABLED:
        return
    await AiJobRepository(ctx.session).enqueue(
        tenant_id=ctx.tenant_id,
        kind=AiJobKind.DELETE_VECTOR,
        payload={"ticket_id": str(ctx.entity_id)},
    )


def register() -> None:
    subscribe(DomainEvent.TICKET_CREATED, _enqueue_embed)
    subscribe(DomainEvent.TICKET_CONTENT_CHANGED, _enqueue_embed)
    subscribe(DomainEvent.TICKET_DELETED, _enqueue_delete)
    logger.info("intelligence subscribers registered")
