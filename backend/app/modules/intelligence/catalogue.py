"""Fetching the ticket text the reranker reads.

Small on purpose. The interesting thing about this file is what it *does not*
select: the ticket's comments, its resolution notes, its client, its assignee.

The reranker is asked one question — is this the same defect — and every extra
field is prompt tokens spent on something that does not help answer it, plus
one more piece of customer data sent to a hosted model. `status` and `closed_at`
are the exceptions, and they are there for a specific reason: "recurring" is a
judgement about a closed ticket coming back, which cannot be made without
knowing the ticket was closed.
"""

from __future__ import annotations

import uuid
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.modules.tickets.models import Ticket


class PostgresTicketCatalogue:
    """Reads candidate text from the tenant-scoped session."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def fetch(self, ticket_ids: list[uuid.UUID]) -> dict[uuid.UUID, dict[str, Any]]:
        if not ticket_ids:
            return {}

        rows = await self._session.execute(
            select(
                Ticket.id,
                Ticket.ticket_number,
                Ticket.title,
                Ticket.description,
                Ticket.status,
                Ticket.closed_at,
            ).where(Ticket.id.in_(ticket_ids), Ticket.deleted_at.is_(None))
        )

        return {
            ticket_id: {
                "reference": f"OS-{number}",
                "title": title,
                # Bounded here rather than in the prompt builder, so the limit
                # is enforced at the boundary and cannot be forgotten by a
                # second caller. Twenty candidates times an unbounded
                # description is how a prompt silently exceeds a context window
                # and the model appears to ignore half its input.
                "description": (description or "")[: settings.RERANK_DESCRIPTION_MAX],
                "status": status.value,
                "closed_at": closed_at.isoformat() if closed_at else None,
            }
            for ticket_id, number, title, description, status, closed_at in rows.all()
        }
