"""Read-only tools over the ticket data.

## The rule these files follow, and why it is not a style preference

**A tool calls an existing service or repository and shapes the result. It
contains no query logic of its own.**

The moment a tool writes its own query it bypasses `policies.py`, the
permission matrix, and every resource rule the application has — and the entire
safety argument for letting a model drive these collapses. The agent is safe
because it can only reach what the repository already exposes to a
tenant-scoped session, not because a prompt told it to behave.

## Read-only is a structural guarantee, not a promise

Nothing here writes. There is no `close_ticket`, no `link_duplicate`, no
`assign`. That is what makes prompt injection survivable: a customer's ticket
description reaches the model, and the worst a successful injection achieves is
a *wrong answer*, because there is no tool that could take an action.

`verify_ai.py` asserts the tool names against an allowlist, so a helpful pull
request adding one write tool fails the check rather than quietly removing the
property everything else depends on.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import NotFoundError
from app.core.logging import get_logger
from app.core.pagination import PageParams
from app.modules.clients.models import Client
from app.modules.comments.models import Comment
from app.modules.identity.models import User, UserTeam
from app.modules.intelligence.tools.schemas import (
    MAX_ROWS,
    CommentsArgs,
    PeopleArgs,
    TicketLookupArgs,
    TicketSearchArgs,
)
from app.modules.teams.models import Team
from app.modules.tickets.enums import Priority, TicketStatus
from app.modules.tickets.models import Ticket
from app.modules.tickets.repository import TicketFilters
from app.modules.tickets.service import TicketService

logger = get_logger(__name__)

#: How much of a description a tool returns.
#:
#: Every character a tool returns becomes prompt tokens on the *next* model
#: call, so this is a cost control as much as a readability one. Enough to
#: judge what a ticket is about; not the whole pasted log.
DESCRIPTION_EXCERPT = 400
COMMENT_EXCERPT = 300


@dataclass(frozen=True, slots=True)
class ToolContext:
    """Who is asking.

    Carried explicitly rather than read from a contextvar, because a tool's
    scope has to be the *caller's* scope and an agent run may outlive the
    request that started it. Passing it makes the coupling visible and makes
    the tools testable with a fake.
    """

    tenant_id: uuid.UUID
    user_id: uuid.UUID
    #: The user's own teams, so "my team's tickets" means something.
    team_ids: tuple[uuid.UUID, ...] = ()


class TicketTools:
    """The read-only ticket toolset, bound to one session and one caller."""

    def __init__(self, session: AsyncSession, ctx: ToolContext) -> None:
        self._session = session
        self._ctx = ctx
        self._tickets = TicketService(session, ctx.tenant_id)

    # ------------------------------------------------------------------ search

    async def search_tickets(self, args: TicketSearchArgs) -> dict[str, Any]:
        """Filter tickets the way the list page does.

        Goes through `TicketRepository.search`, the same call the UI makes, so
        the filters, the sort, and the tenant scoping are the ones already in
        production rather than a second interpretation of them.
        """
        statuses, bad_statuses = _enums(TicketStatus, args.status)
        priorities, bad_priorities = _enums(Priority, args.priority)

        filters = TicketFilters(
            query=args.query,
            statuses=statuses,
            priorities=priorities,
            team_ids=await self._resolve_team(args.team),
            client_ids=await self._resolve_client(args.client),
            assignee_id=await self._resolve_user(args.assignee_email),
            unassigned=args.unassigned,
            created_from=args.created_from,
            created_to=args.created_to,
        )

        page = await self._tickets.search(
            filters, PageParams(page=1, page_size=min(args.limit, MAX_ROWS))
        )
        payload: dict[str, Any] = {
            "total_matching": page.total,
            "returned": len(page.items),
            # Said explicitly, because a model given 10 of 340 rows will
            # otherwise reason as though it has seen all of them and state
            # conclusions about "the tickets" that are true only of the sample.
            "truncated": page.total > len(page.items),
            "tickets": [await _summarise(self._session, ticket) for ticket in page.items],
        }

        # A discarded filter value has to be *reported*, not just dropped.
        # Found by probing with a deliberately malformed status: the value was
        # safely discarded, and the query silently widened from "closed" to all
        # 220 tickets with nothing telling the model its filter had gone. It
        # would then have reasoned confidently about "the closed tickets".
        # Silently ignoring an argument is worse than rejecting it.
        ignored = {}
        if bad_statuses:
            ignored["status"] = bad_statuses
        if bad_priorities:
            ignored["priority"] = bad_priorities
        if args.team and not filters.team_ids:
            ignored["team"] = [args.team]
        if args.client and not filters.client_ids:
            ignored["client"] = [args.client]
        if args.assignee_email and filters.assignee_id is None:
            ignored["assignee_email"] = [args.assignee_email]

        if ignored:
            payload["ignored_filters"] = ignored
            payload["warning"] = (
                "Some filter values were not recognised and had NO effect, so these results "
                "are broader than you asked for. Check the values and try again."
            )
        return payload

    async def get_ticket(self, args: TicketLookupArgs) -> dict[str, Any]:
        """Fetch one ticket in full."""
        ticket = await self._resolve_reference(args.reference)
        detail = await _summarise(self._session, ticket)
        detail.update(
            {
                "description": (ticket.description or "")[:DESCRIPTION_EXCERPT],
                "steps_to_reproduce": (ticket.steps_to_reproduce or "")[:DESCRIPTION_EXCERPT],
                "expected_result": (ticket.expected_result or "")[:200],
                "actual_result": (ticket.actual_result or "")[:200],
                "resolution_notes": (ticket.resolution_notes or "")[:DESCRIPTION_EXCERPT],
                "rejection_reason": ticket.rejection_reason.value
                if ticket.rejection_reason
                else None,
                "reopen_count": ticket.reopen_count,
                "environment": ticket.environment.value,
                "severity": ticket.severity.value,
                "impact": ticket.impact.value,
                "workaround": ticket.workaround.value,
            }
        )
        return detail

    async def get_comments(self, args: CommentsArgs) -> dict[str, Any]:
        """The conversation on a ticket, oldest first.

        Oldest first because a conversation read backwards is nonsense, and
        because when this is truncated the *early* messages are the ones that
        establish what the problem is.
        """
        ticket = await self._resolve_reference(args.reference)
        rows = await self._session.execute(
            select(Comment, User.full_name)
            .join(User, User.id == Comment.author_id)
            .where(Comment.ticket_id == ticket.id, Comment.deleted_at.is_(None))
            .order_by(Comment.created_at)
            .limit(min(args.limit, MAX_ROWS))
        )
        items = rows.all()
        return {
            "reference": ticket.reference,
            "returned": len(items),
            "comments": [
                {
                    "author": author,
                    "at": comment.created_at.isoformat(),
                    "body": comment.body[:COMMENT_EXCERPT],
                }
                for comment, author in items
            ],
        }

    async def get_history(self, args: TicketLookupArgs) -> dict[str, Any]:
        """The status timeline.

        The tool that answers "was this reopened, and how often" — which is the
        difference between a bug that was fixed and a bug that keeps coming
        back, and not something the current status can tell you.
        """
        ticket = await self._resolve_reference(args.reference)
        history = await self._tickets.history(ticket.id)
        return {
            "reference": ticket.reference,
            "reopen_count": ticket.reopen_count,
            "transitions": [
                {
                    "from": entry.from_status.value if entry.from_status else None,
                    "to": entry.to_status.value,
                    "at": entry.changed_at.isoformat(),
                    "note": (entry.note or "")[:160],
                }
                for entry in history[-MAX_ROWS:]
            ],
        }

    async def find_people(self, args: PeopleArgs) -> dict[str, Any]:
        """Resolve a name to the people who have it -- **all** of them.

        Returning every match rather than the best one is the entire point. A
        tool that returned one Divya would let the assistant answer
        confidently about the wrong person, and the reader would have no way to
        tell. Returning two, with `ambiguous: true`, gives the model something
        it can act on: ask which.

        Emails are included because they are how the other tools filter. Role
        and team are included because they are what distinguishes two people
        with the same first name in a way the asker will recognise.
        """
        term = args.name.strip()
        # Team comes through the `user_teams` join table -- membership is
        # many-to-many in the schema even though one team per person is the
        # expectation today. An outer join, so a person with no team still
        # appears: "who is Divya" must not depend on her having been assigned
        # to a team.
        rows = await self._session.execute(
            select(User.full_name, User.email, User.role, Team.name)
            .outerjoin(UserTeam, UserTeam.user_id == User.id)
            .outerjoin(Team, Team.id == UserTeam.team_id)
            .where(
                User.full_name.ilike(f"%{term}%"),
                User.is_active.is_(True),
                User.deleted_at.is_(None),
            )
            .order_by(User.full_name)
            .limit(MAX_ROWS)
        )

        people = [
            {
                "full_name": full_name,
                "email": email,
                "role": role.value,
                "team": team_name,
            }
            for full_name, email, role, team_name in rows.all()
        ]

        return {
            "searched_for": term,
            "matches": len(people),
            # Stated explicitly rather than left for the model to infer from
            # the list length. An explicit flag is harder to overlook, and
            # overlooking it means answering about the wrong person.
            "ambiguous": len(people) > 1,
            "people": people,
            "guidance": (
                "More than one person matches. Ask which one before answering."
                if len(people) > 1
                else ""
            ),
        }

    # ------------------------------------------------------------- resolution

    async def _resolve_reference(self, reference: str) -> Ticket:
        raw = reference[3:] if reference.lower().startswith("os-") else reference
        if not raw.strip().isdigit():
            raise NotFoundError(f"{reference!r} is not a ticket reference.")
        return await self._tickets.get_by_reference(raw.strip())

    async def _resolve_team(self, name: str | None) -> list[uuid.UUID]:
        """Resolve a team name to an id, tolerantly.

        Case-insensitive and partial, because a model will write "payroll" for
        "Payroll" and there is no reason to make that a failure. An unknown
        name resolves to no filter rather than an error -- the tool returns
        "no matches" and the agent can try something else, which is a better
        conversation than an exception.
        """
        if not name:
            return []
        rows = await self._session.execute(
            select(Team.id).where(Team.name.ilike(f"%{name.strip()}%"))
        )
        return list(rows.scalars().all())

    async def _resolve_client(self, name: str | None) -> list[uuid.UUID]:
        if not name:
            return []
        rows = await self._session.execute(
            select(Client.id).where(Client.name.ilike(f"%{name.strip()}%"))
        )
        return list(rows.scalars().all())

    async def _resolve_user(self, email: str | None) -> uuid.UUID | None:
        if not email:
            return None
        found = await self._session.scalar(
            select(User.id).where(User.email == email.strip().lower())
        )
        return found if isinstance(found, uuid.UUID) else None


def _enums(enum_cls: type, values: list[str] | None) -> tuple[list[Any], list[str]]:
    """Convert model-supplied strings to enum members.

    Returns ``(recognised, discarded)``. The second half is the important one:
    an unrecognised value is dropped rather than raising -- a model that writes
    "in-progress" for "in_progress" deserves a retry, not a stack trace -- but
    the caller has to be able to *say* it was dropped. Coercing to an
    allowlisted enum is what makes discarding safe from an injection point of
    view; reporting it is what makes the answer honest.
    """
    if not values:
        return [], []
    recognised: list[Any] = []
    discarded: list[str] = []
    for value in values:
        try:
            recognised.append(enum_cls(value.strip().lower()))
        except ValueError:
            discarded.append(value[:40])
            logger.info("tool discarded an unknown enum value", extra={"value": value[:40]})
    return recognised, discarded


async def _summarise(session: AsyncSession, ticket: Ticket) -> dict[str, Any]:
    """The shape every ticket-returning tool uses.

    One shape, so the model sees consistent field names across every tool. A
    model that gets `title` from one tool and `subject` from another spends
    turns reconciling them instead of answering.

    Names are resolved with ``session.get`` per ticket, the same way
    ``tickets/support.py`` does it. That looks like an N+1 and is not: the
    reference tables are tiny, and SQLAlchemy's identity map means a page of
    twenty tickets from one team issues one query for that team, not twenty.
    Matching the existing convention also matters more than micro-optimising
    here -- two ways of resolving the same names is how they drift.
    """
    client = await session.get(Client, ticket.client_id)
    team = await session.get(Team, ticket.team_id)
    assignee = await session.get(User, ticket.assignee_id) if ticket.assignee_id else None

    return {
        "reference": ticket.reference,
        "title": ticket.title,
        "status": ticket.status.value,
        "priority": ticket.priority.value,
        "created_at": ticket.created_at.isoformat(),
        "closed_at": ticket.closed_at.isoformat() if ticket.closed_at else None,
        "team": team.name if team else None,
        "client": client.name if client else None,
        "assignee": assignee.full_name if assignee else None,
    }
