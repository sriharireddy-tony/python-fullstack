"""Ticket data access.

Queries only. The filter builder here is the hottest path in the application --
it backs every list view and saved view -- so it stays close to the indexes
declared on the model.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import date
from typing import Any

from sqlalchemy import ColumnElement, Select, desc, func, or_, select, text
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import joinedload

from app.core.pagination import Page, PageParams
from app.modules.tickets.enums import AWAITING_CS, Environment, Priority, TicketStatus
from app.modules.tickets.models import Ticket, TicketStatusHistory

#: Sortable columns, as an allowlist.
#:
#: Sort fields reach the ORDER BY clause. An unvalidated one is both an
#: injection surface and a way to force an unindexed sort over the whole table.
SORTABLE: dict[str, Any] = {
    "created_at": Ticket.created_at,
    "updated_at": Ticket.updated_at,
    "priority": Ticket.priority,
    "status": Ticket.status,
    "ticket_number": Ticket.ticket_number,
}


@dataclass(slots=True)
class TicketFilters:
    """Filters accepted by the list endpoint.

    Repeated values mean OR within a field; different fields are ANDed.
    """

    statuses: list[TicketStatus] = field(default_factory=list)
    priorities: list[Priority] = field(default_factory=list)
    team_ids: list[uuid.UUID] = field(default_factory=list)
    client_ids: list[uuid.UUID] = field(default_factory=list)
    assignee_id: uuid.UUID | None = None
    unassigned: bool = False
    awaiting_closure: bool = False
    environment: Environment | None = None
    created_from: date | None = None
    created_to: date | None = None
    query: str | None = None


class TicketRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    def _base(self) -> Select[tuple[Ticket]]:
        # Row-level security already restricts to the tenant; the soft-delete
        # predicate is applied here so no caller can forget it.
        return select(Ticket).where(Ticket.deleted_at.is_(None))

    def _apply(self, stmt: Select[Any], filters: TicketFilters) -> Select[Any]:
        """Apply filters to either the row query or the matching count query.

        Typed loosely on purpose: the same predicates must be applied to
        ``select(Ticket)`` and to ``select(count())``, and any divergence
        between the two would make the reported total disagree with the rows.
        """
        if filters.statuses:
            stmt = stmt.where(Ticket.status.in_(filters.statuses))
        if filters.awaiting_closure:
            stmt = stmt.where(Ticket.status.in_(AWAITING_CS))
        if filters.priorities:
            stmt = stmt.where(Ticket.priority.in_(filters.priorities))
        if filters.team_ids:
            stmt = stmt.where(Ticket.team_id.in_(filters.team_ids))
        if filters.client_ids:
            stmt = stmt.where(Ticket.client_id.in_(filters.client_ids))
        if filters.unassigned:
            stmt = stmt.where(Ticket.assignee_id.is_(None))
        elif filters.assignee_id is not None:
            stmt = stmt.where(Ticket.assignee_id == filters.assignee_id)
        if filters.environment is not None:
            stmt = stmt.where(Ticket.environment == filters.environment)
        if filters.created_from is not None:
            stmt = stmt.where(Ticket.created_at >= filters.created_from)
        if filters.created_to is not None:
            stmt = stmt.where(Ticket.created_at < filters.created_to)

        if filters.query:
            stmt = stmt.where(self._search_condition(filters.query))

        return stmt

    def _search_condition(self, raw: str) -> ColumnElement[bool]:
        """Full-text search, with a shortcut for ticket numbers.

        People quote ticket numbers far more often than they search prose, so
        ``1042`` and ``OS-1042`` match the number directly as well as the text.
        """
        term = raw.strip()
        conditions: list[ColumnElement[bool]] = []

        digits = term[3:] if term.lower().startswith("os-") else term
        if digits.isdigit():
            conditions.append(Ticket.ticket_number == int(digits))

        # websearch_to_tsquery accepts what people actually type -- quoted
        # phrases, OR, minus -- without raising on malformed input the way
        # to_tsquery does.
        conditions.append(Ticket.search_vector.op("@@")(func.websearch_to_tsquery("english", term)))
        return or_(*conditions)

    async def search(
        self,
        filters: TicketFilters,
        params: PageParams,
        *,
        sort_field: str = "created_at",
        descending: bool = True,
    ) -> Page[Ticket]:
        column = SORTABLE.get(sort_field, Ticket.created_at)
        order = desc(column) if descending else column

        count_stmt = self._apply(select(func.count()).select_from(Ticket), filters)
        total = int(await self._session.scalar(count_stmt.where(Ticket.deleted_at.is_(None))) or 0)

        stmt = (
            self._apply(self._base(), filters)
            .order_by(order, desc(Ticket.id))  # id breaks ties so paging is stable
            .offset(params.offset)
            .limit(params.limit)
        )
        result = await self._session.execute(stmt)
        return Page.create(list(result.scalars().unique().all()), total, params)

    async def get(self, ticket_id: uuid.UUID) -> Ticket | None:
        result = await self._session.execute(self._base().where(Ticket.id == ticket_id))
        return result.scalar_one_or_none()

    async def get_by_number(self, ticket_number: int) -> Ticket | None:
        result = await self._session.execute(
            self._base().where(Ticket.ticket_number == ticket_number)
        )
        return result.scalar_one_or_none()

    async def next_ticket_number(self, tenant_id: uuid.UUID) -> int:
        """Allocate the next per-tenant ticket number.

        Locks the counter row for the duration of the transaction. Two agents
        submitting simultaneously with ``MAX(ticket_number) + 1`` would produce
        duplicate numbers -- a race that only appears under real usage.
        """
        row = await self._session.execute(
            text(
                "UPDATE tenant_counters "
                "SET last_ticket_number = last_ticket_number + 1 "
                "WHERE tenant_id = :tenant_id "
                "RETURNING last_ticket_number"
            ),
            {"tenant_id": str(tenant_id)},
        )
        number = row.scalar_one_or_none()
        if number is None:
            # A tenant created before counters existed, or seeded incompletely.
            await self._session.execute(
                text(
                    "INSERT INTO tenant_counters (tenant_id, last_ticket_number) "
                    "VALUES (:tenant_id, 1) "
                    "ON CONFLICT (tenant_id) DO UPDATE "
                    "SET last_ticket_number = tenant_counters.last_ticket_number + 1"
                ),
                {"tenant_id": str(tenant_id)},
            )
            number = await self._session.scalar(
                text("SELECT last_ticket_number FROM tenant_counters WHERE tenant_id = :tenant_id"),
                {"tenant_id": str(tenant_id)},
            )
        return int(number or 1)

    async def add(self, ticket: Ticket) -> Ticket:
        self._session.add(ticket)
        await self._session.flush()
        return ticket

    async def history(self, ticket_id: uuid.UUID) -> list[TicketStatusHistory]:
        result = await self._session.execute(
            select(TicketStatusHistory)
            .where(TicketStatusHistory.ticket_id == ticket_id)
            .order_by(TicketStatusHistory.changed_at)
        )
        return list(result.scalars().all())

    async def counts_for_dashboard(
        self, *, user_id: uuid.UUID, team_ids: list[uuid.UUID]
    ) -> dict[str, int]:
        """Counts behind the dashboard tiles and nav badges.

        One grouped query rather than four round trips.
        """
        mine = await self._session.scalar(
            select(func.count())
            .select_from(Ticket)
            .where(
                Ticket.deleted_at.is_(None),
                Ticket.assignee_id == user_id,
                Ticket.status.in_(
                    [
                        TicketStatus.ASSIGNED,
                        TicketStatus.IN_PROGRESS,
                        TicketStatus.ON_HOLD,
                    ]
                ),
            )
        )

        inbox = 0
        if team_ids:
            inbox = (
                await self._session.scalar(
                    select(func.count())
                    .select_from(Ticket)
                    .where(
                        Ticket.deleted_at.is_(None),
                        Ticket.team_id.in_(team_ids),
                        Ticket.assignee_id.is_(None),
                        Ticket.status == TicketStatus.OPEN,
                    )
                )
                or 0
            )

        awaiting = await self._session.scalar(
            select(func.count())
            .select_from(Ticket)
            .where(Ticket.deleted_at.is_(None), Ticket.status.in_(AWAITING_CS))
        )

        unassigned = await self._session.scalar(
            select(func.count())
            .select_from(Ticket)
            .where(
                Ticket.deleted_at.is_(None),
                Ticket.assignee_id.is_(None),
                Ticket.status == TicketStatus.OPEN,
            )
        )

        return {
            "my_open": mine or 0,
            "team_inbox": inbox,
            "awaiting_closure": awaiting or 0,
            "unassigned": unassigned or 0,
        }

    async def reassign_all(self, from_user_id: uuid.UUID, to_user_id: uuid.UUID | None) -> int:
        """Bulk reassignment, used when deactivating a user.

        Without this, deactivating someone leaves their open tickets assigned to
        an account nobody can act as -- a silent data black hole.
        """
        from typing import cast

        from sqlalchemy import (
            CursorResult,
            update,
        )

        result = cast(
            "CursorResult[Any]",
            await self._session.execute(
                update(Ticket)
                .where(
                    Ticket.assignee_id == from_user_id,
                    Ticket.status.notin_([TicketStatus.CLOSED]),
                )
                .values(
                    assignee_id=to_user_id,
                    status=TicketStatus.OPEN if to_user_id is None else Ticket.status,
                )
            ),
        )
        return result.rowcount or 0


__all__ = ["SORTABLE", "TicketFilters", "TicketRepository", "joinedload"]
