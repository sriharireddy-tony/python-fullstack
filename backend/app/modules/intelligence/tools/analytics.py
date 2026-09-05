"""The bounded aggregation tool.

## Why this is one tool with an allowlist rather than a query interface

"Which team has the most reopened tickets this quarter" is a real question, and
answering it needs a GROUP BY. The tempting way to support that is to let the
model write the grouping. That is text-to-SQL with a smaller blast radius, and
it fails for the same reasons:

* **The dimension reaches a SQL clause.** Even validated against a regex, a
  model-chosen column name is an injection surface and a way to group by an
  unindexed column across the whole table.
* **There is no bound on the work.** `GROUP BY` over an arbitrary expression
  and an arbitrary date range is how one question locks a table.

So the dimensions are an **allowlist of seven**, each mapping to a column that
is either indexed or a foreign key to a small table. The date range is capped.
The result is capped. Every combination the model can ask for is a query whose
cost is known in advance.

The cost of that choice is real and worth stating: the model cannot ask
something the allowlist does not cover, and it will sometimes want to. The
answer is to add a dimension deliberately, having thought about the index — not
to hand out a query language.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import Select, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.logging import get_logger
from app.modules.clients.models import Client
from app.modules.identity.models import User
from app.modules.intelligence.tools.schemas import MAX_ROWS, AnalyticsArgs
from app.modules.teams.models import Team
from app.modules.tickets.enums import TicketStatus
from app.modules.tickets.models import Ticket

logger = get_logger(__name__)

#: dimension name -> (label expression, grouping column, optional join)
#:
#: An allowlist, and the reason it is a table rather than a match statement is
#: that adding a dimension should be one reviewable line where the index
#: question is obvious.
_DIMENSIONS: dict[str, str] = {
    "status": "status",
    "priority": "priority",
    "severity": "severity",
    "environment": "environment",
    "team": "team",
    "client": "client",
    "assignee": "assignee",
}


class AnalyticsTools:
    """Counting tickets along one allowlisted dimension."""

    def __init__(self, session: AsyncSession, tenant_id: uuid.UUID) -> None:
        self._session = session
        self._tenant_id = tenant_id

    async def count_tickets(self, args: AnalyticsArgs) -> dict[str, Any]:
        """Count tickets grouped by one dimension.

        Returns the dimensions it *would* have accepted when given an unknown
        one. A model that asked for "module" gets a list containing "team" and
        can correct itself in the next turn, which costs one cheap round trip
        instead of a failed run.
        """
        dimension = args.dimension.strip().lower()
        if dimension not in _DIMENSIONS:
            return {
                "error": f"Unknown dimension {args.dimension!r}.",
                "allowed_dimensions": sorted(_DIMENSIONS),
            }

        since = datetime.now(UTC) - timedelta(days=args.days)
        label, stmt = self._grouped(dimension, since)

        if args.team:
            # `dimension == "team"` already joined the table; joining twice
            # would raise. Grouping by team *and* filtering to one team is a
            # slightly odd request, and answering it is better than refusing
            # it -- the model sometimes does exactly that on the way to a
            # single number.
            if dimension != "team":
                stmt = stmt.join(Team, Team.id == Ticket.team_id)
            stmt = stmt.where(Team.name.ilike(f"%{args.team.strip()}%"))

        if args.status:
            statuses = []
            for value in args.status[:8]:
                try:
                    statuses.append(TicketStatus(value.strip().lower()))
                except ValueError:
                    continue
            if statuses:
                stmt = stmt.where(Ticket.status.in_(statuses))

        stmt = stmt.group_by(label).order_by(func.count(Ticket.id).desc()).limit(MAX_ROWS)
        rows = await self._session.execute(stmt)
        buckets = [{"value": _text(value), "tickets": count} for value, count in rows.all()]

        total = sum(bucket["tickets"] for bucket in buckets if isinstance(bucket["tickets"], int))

        # The result echoes the filters that produced it.
        #
        # Without this, a filtered count is indistinguishable from an
        # unfiltered one. Asked "how many open tickets does Payroll have", the
        # assistant *did* call this with `team='Payroll'`, got back buckets
        # keyed by status, and replied that the data "only shows counts by
        # status, not by team" -- because nothing in the result said the team
        # filter had been applied. It was reasoning correctly about an
        # under-described result.
        #
        # A tool result has to describe itself. The model cannot see the call
        # it made two steps ago.
        applied: dict[str, Any] = {"created_within_days": args.days}
        if args.team:
            applied["team"] = args.team
        if args.status:
            applied["status"] = args.status

        return {
            "dimension": dimension,
            "filters_applied": applied,
            "total_tickets": total,
            "buckets": buckets,
        }

    async def reopen_rate(self, args: AnalyticsArgs) -> dict[str, Any]:
        """Reopen counts by team.

        A separate tool rather than a `count_tickets` dimension, because
        "reopened" is not a value of a column -- it is a threshold on
        `reopen_count`, and expressing that through a generic grouping
        interface would mean giving the model a comparison operator. This is
        the same trade the whole file makes: a specific tool for a specific
        question beats a general tool that needs a query language.

        It is also the question worth asking of a bug tracker. A high reopen
        rate is the signal that fixes are not holding, which no count of open
        tickets reveals.
        """
        since = datetime.now(UTC) - timedelta(days=args.days)
        rows = await self._session.execute(
            select(
                Team.name,
                func.count(Ticket.id).label("total"),
                func.count(Ticket.id).filter(Ticket.reopen_count > 0).label("reopened"),
            )
            .join(Team, Team.id == Ticket.team_id)
            .where(Ticket.deleted_at.is_(None), Ticket.created_at >= since)
            .group_by(Team.name)
            .order_by(func.count(Ticket.id).filter(Ticket.reopen_count > 0).desc())
            .limit(MAX_ROWS)
        )
        return {
            "filters_applied": {"created_within_days": args.days},
            "teams": [
                {
                    "team": name,
                    "tickets": total,
                    "reopened": reopened,
                    "reopen_rate": round(reopened / total, 3) if total else 0.0,
                }
                for name, total, reopened in rows.all()
            ],
        }

    def _grouped(self, dimension: str, since: datetime) -> tuple[Any, Select[Any]]:
        """Build the grouped select for one allowlisted dimension.

        The joins are written out per dimension rather than derived, because a
        derived join is one more thing a reader has to simulate to know what
        query runs.

        Returns ``Any`` for the label because the three branches produce
        different SQLAlchemy expression types (a mapped column, a joined
        column) that share no useful common supertype. Narrowing it would mean
        a cast per branch that asserts something the checker cannot verify
        anyway.
        """
        base = select().where(Ticket.deleted_at.is_(None), Ticket.created_at >= since)

        if dimension == "team":
            label = Team.name
            stmt = base.join(Team, Team.id == Ticket.team_id)
        elif dimension == "client":
            label = Client.name
            stmt = base.join(Client, Client.id == Ticket.client_id)
        elif dimension == "assignee":
            label = User.full_name
            # An outer join, so unassigned tickets are counted rather than
            # silently vanishing -- "how many are unassigned" is usually the
            # point of asking.
            stmt = base.outerjoin(User, User.id == Ticket.assignee_id)
        else:
            label = getattr(Ticket, dimension)
            stmt = base

        return label, stmt.with_only_columns(label, func.count(Ticket.id)).select_from(Ticket)


def _text(value: Any) -> str:
    if value is None:
        return "unassigned"
    return str(getattr(value, "value", value))
