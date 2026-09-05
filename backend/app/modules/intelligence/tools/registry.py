"""Assembling the toolset for one caller.

``build_tools(session, ctx, ai)`` returns every tool the agent and the chatbot
may use, already bound to a tenant-scoped session and to the caller's identity.

## Why a factory rather than module-level tool functions

Because a tool's scope must be the *caller's* scope. A module-level function
would have to fetch a session and a tenant from somewhere — a contextvar, a
global — and the day an agent run outlives the request that started it, that
somewhere is wrong and the failure is a cross-tenant read.

Closing over the session makes the binding explicit and makes it impossible to
call a tool without having decided whose data it reads.

## The invariant everything else depends on

``READ_ONLY_TOOLS`` is the complete set of names. `verify_ai.py` asserts the
built toolset against it, so adding a tool that writes fails a check rather
than quietly removing the property that makes prompt injection survivable.

That check is the *reason* the agent needs no elaborate output filtering: with
no write tool in existence, a successful injection produces a wrong sentence,
not a closed ticket.
"""

from __future__ import annotations

import json
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

from pydantic import BaseModel, ValidationError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import AppError
from app.core.logging import get_logger
from app.modules.intelligence.deps import AiDeps
from app.modules.intelligence.guardrails.input import redact_pii
from app.modules.intelligence.tools.analytics import AnalyticsTools
from app.modules.intelligence.tools.schemas import (
    AnalyticsArgs,
    CommentsArgs,
    PeopleArgs,
    SimilarSearchArgs,
    TicketLookupArgs,
    TicketSearchArgs,
)
from app.modules.intelligence.tools.tickets import TicketTools, ToolContext

logger = get_logger(__name__)

#: Every tool name, and none of them write.
#:
#: Asserted in verification. A pull request adding `close_ticket` has to change
#: this line, which is exactly the review conversation that should happen.
READ_ONLY_TOOLS = frozenset(
    {
        "search_tickets",
        "get_ticket",
        "get_comments",
        "get_history",
        "find_similar_tickets",
        "count_tickets",
        "reopen_rate",
        "find_people",
    }
)

#: Hard cap on the serialised size of any tool result.
#:
#: The bound that matters most, and the least obvious. A tool result is fed
#: straight back into the model's context, so an unbounded result is an
#: unbounded prompt — a cost incident and a context overflow, not a slow query.
#: Every tool bounds its rows already; this is the backstop for the case where
#: twenty-five rows of long text still add up.
MAX_RESULT_CHARS = 6000


@dataclass(frozen=True, slots=True)
class Tool:
    """One callable tool, with the schema its arguments must satisfy.

    ``name``, ``description`` and ``args_schema`` are what get advertised to the
    model; ``run`` is what executes. Keeping them in one object means the thing
    the model was told about and the thing that runs cannot drift apart.
    """

    name: str
    description: str
    args_schema: type[BaseModel]
    run: Callable[[BaseModel], Awaitable[dict[str, Any]]]

    async def invoke(self, raw: dict[str, Any]) -> dict[str, Any]:
        """Validate the model's arguments, run, and bound the result.

        Every failure mode returns a **dict the model can read**, never an
        exception. That is the difference between an agent that recovers and an
        agent that dies: told "days must be <= 365", it retries with 365; given
        a stack trace, the run ends.
        """
        try:
            args = self.args_schema.model_validate(raw)
        except ValidationError as exc:
            # The model chose bad arguments. Hand back the specific problem so
            # the next turn can fix it, bounded so a large validation error
            # cannot itself blow up the prompt.
            logger.info(
                "tool arguments rejected",
                extra={"tool": self.name, "errors": exc.error_count()},
            )
            return {
                "error": "invalid_arguments",
                "detail": json.dumps(exc.errors(include_url=False))[:800],
            }

        try:
            result = await self.run(args)
        except AppError as exc:
            # An expected domain failure -- a reference that does not exist, a
            # permission denied. Also returned as data: "OS-9999 not found" is
            # information the agent should reason about.
            return {"error": type(exc).__name__, "detail": str(exc)[:400]}
        except Exception:
            # An unexpected failure. Logged with a traceback for us, and
            # reported to the model without internals: an exception message can
            # carry a table name or a query fragment, and the model's output
            # may reach a user.
            logger.exception("tool raised", extra={"tool": self.name})
            return {"error": "tool_failed", "detail": "The tool could not complete."}

        return sanitize_tool_output(result, tool=self.name)


def sanitize_tool_output(result: dict[str, Any], *, tool: str) -> dict[str, Any]:
    """Bound and redact a tool result before it re-enters the prompt.

    Two things happen here, for two different reasons.

    **Redaction**, because tool output is the one path by which raw database
    text reaches a hosted model *without* passing the input guardrail. The
    user's question was redacted on the way in; a ticket description fetched
    mid-loop was not. Missing this is the most likely way this system would
    leak a customer's phone number to a third party.

    **Bounding**, because the result becomes prompt tokens. Truncation is
    reported in the payload rather than done silently: a model told
    ``"truncated": true`` can ask for less, while one handed a quietly cut list
    reasons confidently about data it never saw.
    """
    payload = json.dumps(result, separators=(",", ":"), default=str)
    redacted, counts = redact_pii(payload)

    if counts:
        logger.info(
            "tool output redacted",
            extra={"tool": tool, "redactions": counts},
        )

    if len(redacted) <= MAX_RESULT_CHARS:
        return dict(json.loads(redacted))

    logger.info(
        "tool output truncated",
        extra={"tool": tool, "chars": len(redacted), "limit": MAX_RESULT_CHARS},
    )
    return {
        "truncated": True,
        "reason": (
            f"The result was {len(redacted)} characters, over the {MAX_RESULT_CHARS} limit. "
            "Narrow the query -- fewer rows, or a shorter date range."
        ),
        "excerpt": redacted[:MAX_RESULT_CHARS],
    }


def build_tools(session: AsyncSession, ctx: ToolContext, ai: AiDeps | None = None) -> list[Tool]:
    """Every tool available to one caller.

    The descriptions are written for the model, not for a developer. They say
    *when to use this* rather than *what it does*, because "when" is the
    decision the model is actually making and a description that only restates
    the name gives it nothing to choose on.
    """
    tickets = TicketTools(session, ctx)
    analytics = AnalyticsTools(session, ctx.tenant_id)

    tools = [
        Tool(
            name="search_tickets",
            description=(
                "Find tickets by text, status, priority, team, client, assignee, or date. "
                "Use this to answer questions about groups of tickets, or to find a ticket "
                "whose reference you do not know. Returns at most 25."
            ),
            args_schema=TicketSearchArgs,
            run=lambda args: tickets.search_tickets(args),  # type: ignore[arg-type]
        ),
        Tool(
            name="get_ticket",
            description=(
                "Read one ticket in full by its reference, such as OS-1042. Use this when "
                "you need the description, steps to reproduce, or resolution notes, which "
                "search does not return."
            ),
            args_schema=TicketLookupArgs,
            run=lambda args: tickets.get_ticket(args),  # type: ignore[arg-type]
        ),
        Tool(
            name="get_comments",
            description=(
                "Read the conversation on a ticket, oldest first. Use this to find what "
                "was investigated or asked, which is often not in the description."
            ),
            args_schema=CommentsArgs,
            run=lambda args: tickets.get_comments(args),  # type: ignore[arg-type]
        ),
        Tool(
            name="get_history",
            description=(
                "Read a ticket's status timeline and how many times it was reopened. Use "
                "this to tell a bug that was fixed from one that keeps coming back."
            ),
            args_schema=TicketLookupArgs,
            run=lambda args: tickets.get_history(args),  # type: ignore[arg-type]
        ),
        Tool(
            name="find_people",
            description=(
                "Resolve a person's name to the people who have it, with their email, "
                "role, and team. Use this BEFORE filtering tickets by a person, because "
                "several people can share a first name -- if it returns more than one "
                "match, ask which one instead of choosing."
            ),
            args_schema=PeopleArgs,
            run=lambda args: tickets.find_people(args),  # type: ignore[arg-type]
        ),
        Tool(
            name="count_tickets",
            description=(
                "Count tickets grouped by status, priority, severity, environment, team, "
                "client, or assignee, over a date window. Use this for 'how many' and "
                "'which team has the most' questions instead of searching and counting."
            ),
            args_schema=AnalyticsArgs,
            run=lambda args: analytics.count_tickets(args),  # type: ignore[arg-type]
        ),
        Tool(
            name="reopen_rate",
            description=(
                "Reopen counts and rates by team over a date window. Use this to answer "
                "whether fixes are holding."
            ),
            args_schema=AnalyticsArgs,
            run=lambda args: analytics.reopen_rate(args),  # type: ignore[arg-type]
        ),
    ]

    if ai is not None:
        # The agent's most valuable tool *is* the similarity pipeline. Wired in
        # rather than reimplemented, so it inherits the retrieval that was
        # measured in Phase B and the guardrails from Phase C. Reranking is off
        # here: the agent is already reasoning about the results, so paying for
        # a model to pre-judge them would be two models doing one job.
        from app.modules.intelligence.service import IntelligenceService

        service = IntelligenceService(session, ctx.tenant_id, ai)

        async def find_similar(args: BaseModel) -> dict[str, Any]:
            typed = SimilarSearchArgs.model_validate(args, from_attributes=True)
            result = await service.similar_to_text(
                typed.title, typed.description, limit=typed.limit, with_rerank=False
            )
            return {
                "returned": len(result.results),
                "tickets": [
                    {
                        "reference": item.reference,
                        "title": item.title,
                        "status": item.status,
                        "closed_at": item.closed_at.isoformat() if item.closed_at else None,
                        "resolution": item.resolution_summary,
                        "found_by": [source.value for source in item.ranks],
                    }
                    for item in result.results
                ],
            }

        tools.append(
            Tool(
                name="find_similar_tickets",
                description=(
                    "Find earlier tickets describing the same problem as some text, using "
                    "both meaning-based and keyword search. Use this to check whether a "
                    "problem has been reported or fixed before -- it finds tickets worded "
                    "completely differently, which search_tickets will not."
                ),
                args_schema=SimilarSearchArgs,
                run=find_similar,
            )
        )

    names = {tool.name for tool in tools}
    if not names <= READ_ONLY_TOOLS:
        # A guard against the toolset drifting from the allowlist. Raised at
        # build time rather than checked in a test only, because the property
        # is a security property and should fail closed on the running system.
        raise RuntimeError(
            f"tools not in the read-only allowlist: {sorted(names - READ_ONLY_TOOLS)}"
        )

    return tools


def tool_context(
    tenant_id: uuid.UUID, user_id: uuid.UUID, team_ids: tuple[uuid.UUID, ...] = ()
) -> ToolContext:
    return ToolContext(tenant_id=tenant_id, user_id=user_id, team_ids=team_ids)


def describe_tool(tool: Tool, *, max_args: int = 8) -> str:
    """Render one tool for a prompt, **including its parameter descriptions**.

    This function exists because of a bug that cost an afternoon. The prompts
    listed each tool as ``name(arg1, arg2, arg3)`` -- names only. Every
    ``Field(description=...)`` in `tools/schemas.py` was written, reviewed, and
    never sent to the model.

    The symptom looked like a model problem: asked "how many open tickets does
    Payroll have", the assistant called the analytics tool, got a whole-tracker
    count, and replied that it could not break the data down by team. It was
    right -- it had no way to know a `team` parameter existed, let alone what
    `days` filtered on. Two rounds of tool redesign went into a problem whose
    cause was that the tool contract was never transmitted.

    A tool description is not documentation for developers. It is the interface
    the model programs against, and an interface the caller cannot see is not
    an interface.
    """
    schema = tool.args_schema.model_json_schema()
    properties: dict[str, Any] = schema.get("properties", {})
    required = set(schema.get("required", []))

    lines = [f"- {tool.name}: {tool.description}"]
    for name, spec in list(properties.items())[:max_args]:
        description = str(spec.get("description", "")).strip()
        marker = " (required)" if name in required else ""
        if description:
            lines.append(f"    {name}{marker}: {description}")
        else:
            lines.append(f"    {name}{marker}")
    return "\n".join(lines)
