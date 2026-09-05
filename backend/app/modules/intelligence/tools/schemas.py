"""Tool arguments, as validated schemas.

## Why the agent's tools take a Pydantic model rather than free arguments

Because the arguments come from a language model, and a language model will
eventually send `limit=100000`, `status="closed'; DROP TABLE"`, or
`days=999999`. Validating at the boundary means every one of those is a clean
rejection the agent can read and retry, rather than a query that runs.

This is also where the **text-to-filter** design lives, and it is worth naming
the alternative it rejects. Text-to-SQL is the obvious way to let a model query
a database, and it is a poor fit here for three reasons:

1. **It cannot be bounded.** A generated `SELECT` can join, scan, or aggregate
   anything the connection can reach. The only real defence is a read-only
   replica plus statement timeouts, which is infrastructure, not a guarantee.
2. **It bypasses the application's own rules.** Every safety property this
   codebase has — row-level security scoping, the permission matrix, the
   resource policies — lives above SQL. A model writing SQL steps around all
   of it.
3. **It is unnecessary.** The questions people actually ask a bug tracker are
   filters and counts, and those fit a fixed schema. Text-to-**filter** gets
   the same answers through the same repository the UI uses, which means the
   same tenant scoping and the same permission checks.

So the model chooses *values for a fixed set of fields*. It cannot express a
query the UI could not express, which is precisely the point.
"""

from __future__ import annotations

from datetime import date
from typing import Annotated

from pydantic import BaseModel, Field, field_validator

#: Ceiling on rows any tool returns.
#:
#: Two reasons, and the second is the one people forget. A bounded result keeps
#: the database work predictable — and it keeps the *prompt* bounded, because
#: every row a tool returns is fed straight back into the model's context. An
#: unbounded tool result is an unbounded prompt, which is a cost incident and a
#: context overflow rather than a slow query.
MAX_ROWS = 25

#: How far back an aggregation may look. A year is more history than this
#: tracker has; the cap exists so "all time" cannot become a full-table scan.
MAX_DAYS = 365


class TicketSearchArgs(BaseModel):
    """Arguments for searching tickets.

    Every field maps to something the ticket list UI can already do. That is
    the constraint that makes this safe: the agent has no way to ask a question
    a person could not ask through the interface.
    """

    query: str | None = Field(
        default=None,
        max_length=300,
        description="Free text to match against title, description, and steps.",
    )
    status: list[str] | None = Field(
        default=None,
        description=(
            "Filter by status: open, assigned, in_progress, on_hold, resolved, "
            "rejected, closed. Several means OR."
        ),
    )
    priority: list[str] | None = Field(
        default=None, description="Filter by priority: p1, p2, p3, p4. Several means OR."
    )
    team: str | None = Field(
        default=None, max_length=80, description="Team name, e.g. Payroll or CoreHR."
    )
    client: str | None = Field(default=None, max_length=120, description="Client name.")
    assignee_email: str | None = Field(default=None, max_length=200)
    unassigned: bool = False
    created_from: date | None = None
    created_to: date | None = None
    limit: Annotated[int, Field(ge=1, le=MAX_ROWS)] = 10

    @field_validator("status", "priority")
    @classmethod
    def _bound_lists(cls, value: list[str] | None) -> list[str] | None:
        """Cap the number of OR values.

        A model that sends every status is not filtering, and the resulting
        query is a full scan wearing a filter's clothes.
        """
        if value is None:
            return None
        return value[:8]


class TicketLookupArgs(BaseModel):
    """Fetch one ticket by its human reference."""

    reference: str = Field(
        max_length=20,
        description="Ticket reference such as OS-1042, or the bare number.",
    )


class SimilarSearchArgs(BaseModel):
    """Find earlier tickets resembling some text.

    Wraps the same pipeline the similar-issues panel uses, which is the point:
    the agent's most valuable tool is the retrieval work already built and
    measured, not a second implementation of it.
    """

    title: str = Field(max_length=300, description="A short description of the problem.")
    description: str | None = Field(default=None, max_length=2000)
    limit: Annotated[int, Field(ge=1, le=10)] = 5


class AnalyticsArgs(BaseModel):
    """Arguments for the bounded aggregation tool.

    Deliberately not "group by anything". The dimensions are an allowlist,
    because `group_by` reaching a SQL clause from a model is the same
    injection surface as text-to-SQL with extra steps -- and because an
    unindexed grouping is how one question locks a table.
    """

    dimension: str = Field(
        description=(
            "What to count by: status, priority, team, client, assignee, environment, or severity."
        )
    )
    #: Defaults to the whole tracker, not to a recent window.
    #:
    #: It defaulted to 90 days first, and that was wrong in a way worth
    #: recording: asked "how many open tickets does Payroll have right now",
    #: the model counted a 90-day window and then told the user its answer was
    #: unreliable because of the window. A default that quietly narrows the
    #: data makes the model *correctly* hedge about an answer it should have
    #: been able to give. Broad by default; narrow on request.
    days: Annotated[int, Field(ge=1, le=MAX_DAYS)] = Field(
        default=MAX_DAYS,
        description=(
            "Only count tickets CREATED in the last N days. A filter on creation "
            "date, not a limit on what you can see. Leave it alone unless the "
            "question is about a time period."
        ),
    )
    status: list[str] | None = Field(
        default=None,
        description=(
            "Restrict to these statuses, e.g. ['open'] or "
            "['open','assigned','in_progress'] for work not yet finished."
        ),
    )
    #: A team filter, added because the tool could not answer an obvious
    #: question without it. "How many open tickets does Payroll have" needed
    #: `dimension='team'` plus a status filter and then reading one bucket out
    #: of six -- three inferences the model got wrong. A filter the question
    #: maps onto directly is better tool design than a prompt explaining how to
    #: combine two that do not.
    team: str | None = Field(
        default=None,
        max_length=80,
        description="Restrict to one team, e.g. Payroll. Use with dimension='status'.",
    )


class PeopleArgs(BaseModel):
    """Look up people by name.

    ## Why this tool exists at all

    Because "what is Divya working on?" has two answers in this workspace, and
    the assistant answered one of them without saying so. There are two people
    called Divya; it picked one and reported confidently.

    The instinct is to fix that in the prompt -- "ask when a name is
    ambiguous". That does not work, because the model had **no way to discover
    the ambiguity**: ticket search filters by assignee *email*, and nothing
    mapped a first name to the people who have it. It was not ignoring an
    instruction, it was guessing because guessing was the only option.

    So ambiguity becomes data. This returns **every** match, and the count is
    what the model reasons about.
    """

    name: str = Field(
        max_length=120,
        description="A full or partial name, e.g. 'Divya' or 'Priya Krishnan'.",
    )


class CommentsArgs(BaseModel):
    """Read the conversation on one ticket."""

    reference: str = Field(max_length=20)
    limit: Annotated[int, Field(ge=1, le=MAX_ROWS)] = 10
