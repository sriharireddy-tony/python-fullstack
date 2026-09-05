"""In-transaction domain events.

Solves one specific problem: the tickets module must trigger an embedding job
without importing the intelligence module. Dependencies point one way, and
`tickets` predates the AI layer and should not know it exists.

So `tickets` **emits**, `intelligence` **subscribes**, and the wiring happens
once at startup.

Handlers run **synchronously inside the emitter's transaction**, and that is the
point rather than a limitation: an outbox row written by a handler commits or
rolls back with the ticket itself. A handler that did its own I/O or committed
separately would reintroduce the dual write this exists to avoid.
"""

from __future__ import annotations

import uuid
from collections import defaultdict
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from enum import StrEnum
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.logging import get_logger

logger = get_logger(__name__)


class DomainEvent(StrEnum):
    TICKET_CREATED = "ticket.created"
    TICKET_CONTENT_CHANGED = "ticket.content_changed"
    TICKET_DELETED = "ticket.deleted"


@dataclass(frozen=True, slots=True)
class EventContext:
    """What a handler receives.

    The session is the emitter's, so anything the handler writes is part of the
    same transaction.
    """

    session: AsyncSession
    tenant_id: uuid.UUID
    entity_id: uuid.UUID
    payload: dict[str, Any]


Handler = Callable[[EventContext], Awaitable[None]]

_handlers: dict[DomainEvent, list[Handler]] = defaultdict(list)


def subscribe(event: DomainEvent, handler: Handler) -> None:
    """Register a handler. Called once, at startup."""
    if handler not in _handlers[event]:
        _handlers[event].append(handler)


def clear_subscribers() -> None:
    """Reset the registry. For tests — a leaked handler between test cases
    produces failures that look like anything but a leaked handler."""
    _handlers.clear()


async def emit(
    event: DomainEvent,
    session: AsyncSession,
    tenant_id: uuid.UUID,
    entity_id: uuid.UUID,
    **payload: Any,
) -> None:
    """Fire an event to every subscriber.

    A handler that raises is **logged and swallowed**. That is deliberate: the
    business action must not fail because an optional side effect did. A ticket
    creation should never be rejected because the AI layer is misconfigured —
    the reconciliation job exists precisely to pick up what was missed.
    """
    handlers = _handlers.get(event, [])
    if not handlers:
        return

    context = EventContext(
        session=session, tenant_id=tenant_id, entity_id=entity_id, payload=payload
    )
    for handler in handlers:
        try:
            await handler(context)
        except Exception:
            logger.exception(
                "event handler failed",
                extra={"event": event.value, "handler": getattr(handler, "__name__", "?")},
            )
