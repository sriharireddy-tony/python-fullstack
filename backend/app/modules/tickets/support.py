"""Serialisation helpers shared by the ticket routes.

Kept out of the router so the HTTP layer stays thin, and out of the service so
the service stays free of response-shape concerns.
"""

from __future__ import annotations

import uuid
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.permissions import Permission
from app.modules.clients.models import Client
from app.modules.identity.dependencies import RequestContext
from app.modules.identity.models import User, UserTeam
from app.modules.teams.models import Team
from app.modules.tickets.models import Ticket
from app.modules.tickets.policies import Actor
from app.modules.tickets.schemas import NamedRef, TicketDetail, TicketListItem, UserRef


async def actor_from_context(db: AsyncSession, ctx: RequestContext) -> Actor:
    """Build the policy Actor for the current request.

    Team membership is loaded here because several resource rules depend on it
    -- a developer may only take work from their own team's inbox, a manager may
    only assign within teams they manage.
    """
    result = await db.execute(select(UserTeam.team_id).where(UserTeam.user_id == ctx.user_id))
    team_ids = frozenset(result.scalars().all())

    return Actor(
        user_id=ctx.user_id,
        role=ctx.role,
        permissions=frozenset(Permission(p) for p in ctx.permissions),
        team_ids=team_ids,
    )


async def _refs(
    db: AsyncSession, ticket: Ticket
) -> tuple[NamedRef | None, NamedRef | None, UserRef | None, UserRef | None]:
    """Resolve the display names a ticket row needs.

    Loaded per ticket rather than eagerly joined so the list query stays a
    single indexed scan; the reference tables are tiny and cached upstream.
    """
    client = await db.get(Client, ticket.client_id)
    team = await db.get(Team, ticket.team_id)
    assignee = await db.get(User, ticket.assignee_id) if ticket.assignee_id else None
    creator = await db.get(User, ticket.created_by_id) if ticket.created_by_id else None

    return (
        NamedRef(id=client.id, name=client.name) if client else None,
        NamedRef(id=team.id, name=team.name) if team else None,
        UserRef(id=assignee.id, full_name=assignee.full_name) if assignee else None,
        UserRef(id=creator.id, full_name=creator.full_name) if creator else None,
    )


def _base_fields(ticket: Ticket) -> dict[str, Any]:
    return {
        "id": ticket.id,
        "ticket_number": ticket.ticket_number,
        "title": ticket.title,
        "status": ticket.status,
        "priority": ticket.priority,
        "severity": ticket.severity,
        "impact": ticket.impact,
        "environment": ticket.environment,
        "created_at": ticket.created_at,
        "updated_at": ticket.updated_at,
        "reopen_count": ticket.reopen_count,
        "version": ticket.version,
    }


async def to_list_item(db: AsyncSession, ticket: Ticket) -> TicketListItem:
    client, team, assignee, _creator = await _refs(db, ticket)
    return TicketListItem(**_base_fields(ticket), client=client, team=team, assignee=assignee)


async def to_detail(db: AsyncSession, ticket: Ticket) -> TicketDetail:
    client, team, assignee, creator = await _refs(db, ticket)
    return TicketDetail(
        **_base_fields(ticket),
        client=client,
        team=team,
        assignee=assignee,
        created_by=creator,
        description=ticket.description,
        steps_to_reproduce=ticket.steps_to_reproduce,
        expected_result=ticket.expected_result,
        actual_result=ticket.actual_result,
        workaround=ticket.workaround,
        reporter_name=ticket.reporter_name,
        reporter_email=ticket.reporter_email,
        resolution_notes=ticket.resolution_notes,
        rejection_reason=ticket.rejection_reason,
        on_hold_waiting_on=ticket.on_hold_waiting_on,
        ado_work_item_url=ticket.ado_work_item_url,
        priority_overridden=ticket.priority_overridden,
        priority_override_reason=ticket.priority_override_reason,
        resolved_at=ticket.resolved_at,
        closed_at=ticket.closed_at,
        available_actions=[],
    )


__all__ = ["actor_from_context", "to_detail", "to_list_item", "uuid"]
