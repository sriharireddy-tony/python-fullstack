"""The analysis agent: a ReAct loop as a state machine.

```
        START
          |
        prime                         fetch the ticket -- no LLM call
          |
        decide  <──────────────┐      one LLM call: thought + action
          |                    │
    [action?]                  │
      |     \\                  │
  use_tool  finish             │
      |        \\               │
    act ───────┼───────────────┘      run one tool, record the observation
               |
            report                    one LLM call: the structured report
               |
              END
```

## Why write the loop instead of using `create_react_agent`

LangGraph ships a prebuilt ReAct agent, and for a prototype it is the right
choice. It is the wrong choice here for one reason that outweighs the
convenience: **the limits.**

This run needs a turn cap, a wall-clock deadline, a cost cap, and a persisted
step log — and every one of those has to be enforced *between* turns, with a
partial report written when it fires. Bolting that onto a prebuilt loop means
fighting its control flow; writing the loop makes each limit two lines in a
router function that a reader can check.

The second reason is that a run that stops at a limit must still **produce
something**. `route_after_decide` sends a run that has run out of budget to
`report`, not to `END`, so it writes what it learned and marks itself partial.
An analysis that says "I found two related tickets and ran out of time" is
useful; a run that vanishes is not.

## What stops it running away

Four independent limits, because each catches what the others miss:

| limit | catches |
|---|---|
| turn cap | a model that will not converge |
| wall clock | turns that are individually slow |
| cost cap | a loop that is cheap per turn and long |
| repeat detection | the same tool called with the same arguments |

The turn cap alone is not enough: twelve turns at forty seconds each is eight
minutes. The wall clock alone is not enough either: a fast infinite loop burns
quota inside the deadline.

Repeat detection is the one usually left out, and it is the most common real
failure — an agent that searches, gets nothing, and searches identically again.
Rather than let it burn the budget, the loop tells it plainly that it already
made that call, which is information it can act on.
"""

from __future__ import annotations

import json
import operator
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from functools import lru_cache
from typing import Annotated, Any, TypedDict

from langgraph.graph import END, START, StateGraph
from langgraph.graph.state import CompiledStateGraph
from langgraph.runtime import Runtime

from app.core.errors import AppError
from app.core.logging import get_logger
from app.modules.intelligence.llm.registry import ModelRegistry, ModelRole
from app.modules.intelligence.ports import ChatModel
from app.modules.intelligence.prompts import ANALYSIS_SYSTEM
from app.modules.intelligence.schemas.reports import AgentDecision, AnalysisReport
from app.modules.intelligence.security.budget import Wallclock
from app.modules.intelligence.tools.registry import Tool, describe_tool
from app.modules.intelligence.utils.graph import adapt
from app.modules.intelligence.utils.references import references_in

logger = get_logger(__name__)

#: Defined in `prompts/`, so it can be versioned and diffed
#: independently of the control flow that uses it.
SYSTEM = ANALYSIS_SYSTEM


class AnalysisState(TypedDict, total=False):
    """State for one agent run."""

    tenant_id: uuid.UUID
    run_id: uuid.UUID
    ticket_reference: str
    ticket_id: uuid.UUID

    #: Grows by one entry per turn. `operator.add` because every turn appends
    #: and nothing replaces -- the sequence *is* the audit trail.
    observations: Annotated[list[dict[str, Any]], operator.add]
    #: References the agent has actually seen in tool output. The allowlist the
    #: report's citations are validated against.
    seen_references: Annotated[list[str], operator.add]

    turn: int
    tool_calls: int
    #: Signatures of tool calls already made, for repeat detection.
    call_signatures: Annotated[list[str], operator.add]

    decision: AgentDecision | None
    report: AnalysisReport | None
    partial: bool
    stop_reason: str
    model: str
    estimated_cost: float
    trace: Annotated[list[str], operator.add]


@dataclass(slots=True)
class AgentDeps:
    """What the agent graph needs, injected.

    A dataclass rather than a Protocol because LangGraph's ``context_schema``
    accepts a dataclass, TypedDict, or Pydantic model and rejects a Protocol.
    Testability is unaffected: the fields are typed against ports and
    callables, so a test constructs one with fakes exactly as it would a
    Protocol implementation.
    """

    registry: ModelRegistry
    tools: dict[str, Tool]
    clock: Wallclock
    #: Called after each turn to persist a step row. Injected rather than
    #: imported, so the graph never reaches for a session it does not own --
    #: which is what lets the same graph run inside a request or a worker.
    record_step: Callable[[int, str | None, dict[str, Any] | None, str], Awaitable[None]]
    max_turns: int = 8
    max_tool_calls: int = 12
    #: Cost ceiling for one analysis, in the same units as `ai_usage`.
    max_cost: float = 0.05


Node = Callable[[AnalysisState, AgentDeps], Awaitable[dict[str, object]]]


async def prime(state: AnalysisState, deps: AgentDeps) -> dict[str, object]:
    """Fetch the ticket under analysis. No LLM call.

    ## Why the first turn is not the model's decision

    Two reasons, and the second one is a correctness issue rather than an
    optimisation.

    **Cost.** Every run needs the ticket text. Spending an LLM call on the
    model deciding to fetch the ticket it was given is paying to be told the
    obvious, and on a 500-call daily budget that is one call in four for a
    four-turn run.

    **Evidence.** A run was observed finishing on turn one with `tool_calls=0`
    -- the model went straight to `finish` and wrote a report from nothing but
    the ticket reference in its prompt. The report had a summary, an area, and
    a confidence, all invented. Requiring at least one gathered observation
    would need a guardrail with a retry loop; making the first fetch
    unconditional removes the failure mode instead of policing it.

    So the loop starts with the ticket already in hand, and the model's first
    real decision is what to do *about* it.
    """
    tool = deps.tools.get("get_ticket")
    reference = state.get("ticket_reference", "")
    if tool is None or not reference:
        return {"trace": ["prime:skipped"]}

    result = await tool.invoke({"reference": reference})
    await deps.record_step(0, "get_ticket", {"reference": reference}, _summarise_result(result))

    return {
        "observations": [
            {"tool": "get_ticket", "arguments": {"reference": reference}, "result": result}
        ],
        "seen_references": _references_in(result),
        "call_signatures": [f"get_ticket:{json.dumps({'reference': reference}, sort_keys=True)}"],
        "tool_calls": 1,
        "trace": ["prime:get_ticket"],
    }


async def decide(state: AnalysisState, deps: AgentDeps) -> dict[str, object]:
    """One LLM call: what to do next.

    The prompt carries the observations so far rather than a raw message
    history. That is a deliberate difference from a chat-style agent: a
    summarised observation list stays bounded as the run grows, while a message
    history grows without limit and eventually costs more per turn than the
    work it is describing.
    """
    turn = state.get("turn", 0) + 1
    prompt = _decide_prompt(state, deps)

    async def run(model: ChatModel) -> AgentDecision:
        result = await model.generate_structured(
            SYSTEM, [{"role": "user", "content": prompt}], AgentDecision
        )
        return AgentDecision.model_validate(result, from_attributes=True)

    try:
        outcome = await deps.registry.call(
            ModelRole.AGENT, feature="analysis.decide", run=run, prompt_text=prompt
        )
    except AppError as exc:
        # The model is unavailable or over budget. Not an exception out of the
        # graph: the run finishes with whatever it has and says why.
        logger.warning("agent decide failed", extra={"error": type(exc).__name__})
        return {
            "turn": turn,
            "partial": True,
            "stop_reason": f"the model was unavailable ({type(exc).__name__})",
            "decision": None,
            "trace": [f"decide:{turn}:unavailable"],
        }

    decision: AgentDecision = outcome.value
    return {
        "turn": turn,
        "decision": decision,
        "model": outcome.model,
        "estimated_cost": state.get("estimated_cost", 0.0) + outcome.estimated_cost,
        "trace": [f"decide:{turn}:{decision.action}:{decision.tool or '-'}"],
    }


async def act(state: AnalysisState, deps: AgentDeps) -> dict[str, object]:
    """Run one tool and record what it returned.

    Every failure here becomes an observation rather than an exception, because
    "that tool call was wrong, and here is why" is exactly what the next turn
    needs. An agent that dies on a bad argument cannot recover; one that reads
    the error can.
    """
    decision = state.get("decision")
    turn = state.get("turn", 0)
    if decision is None or decision.tool is None:
        return {"trace": [f"act:{turn}:no-tool"]}

    tool = deps.tools.get(decision.tool)
    if tool is None:
        observation = {
            "tool": decision.tool,
            "error": "unknown_tool",
            "available_tools": sorted(deps.tools),
        }
        await deps.record_step(turn, decision.tool, None, "unknown tool")
        return {
            "observations": [observation],
            "trace": [f"act:{turn}:unknown-tool"],
        }

    try:
        arguments = json.loads(decision.arguments_json or "{}")
        if not isinstance(arguments, dict):
            raise ValueError("arguments must be a JSON object")
    except (json.JSONDecodeError, ValueError) as exc:
        observation = {
            "tool": decision.tool,
            "error": "invalid_json",
            "detail": f"arguments_json was not a JSON object: {exc}",
        }
        await deps.record_step(turn, decision.tool, None, "invalid arguments json")
        return {"observations": [observation], "trace": [f"act:{turn}:bad-json"]}

    signature = f"{decision.tool}:{json.dumps(arguments, sort_keys=True)}"
    if signature in state.get("call_signatures", []):
        # Repeat detection. The most common way an agent wastes a budget is
        # making the same call twice and hoping. Telling it so is cheaper than
        # letting it, and it is information rather than a refusal.
        observation = {
            "tool": decision.tool,
            "error": "repeated_call",
            "detail": (
                "You already made this exact call and have its result above. "
                "Either use a different tool or different arguments, or finish."
            ),
        }
        await deps.record_step(turn, decision.tool, arguments, "repeated call")
        return {
            "observations": [observation],
            "trace": [f"act:{turn}:repeat"],
        }

    result = await tool.invoke(arguments)
    references = _references_in(result)

    await deps.record_step(turn, decision.tool, arguments, _summarise_result(result))

    return {
        "observations": [{"tool": decision.tool, "arguments": arguments, "result": result}],
        "seen_references": references,
        "call_signatures": [signature],
        "tool_calls": state.get("tool_calls", 0) + 1,
        "trace": [f"act:{turn}:{decision.tool}:{len(references)}refs"],
    }


async def report(state: AnalysisState, deps: AgentDeps) -> dict[str, object]:
    """One LLM call: the structured report.

    Separate from `decide` rather than letting the final decision carry the
    report, for two reasons. The report has its own schema and its own
    validation, and mixing it into the decision schema would mean every turn
    paying for fields it does not use. And a run stopped by a limit reaches
    this node too, so the report is written from whatever was gathered rather
    than being lost.
    """
    observations = state.get("observations", [])
    allowed = set(state.get("seen_references", []))
    prompt = _report_prompt(state, observations)

    async def run(model: ChatModel) -> AnalysisReport:
        result = await model.generate_structured(
            SYSTEM, [{"role": "user", "content": prompt}], AnalysisReport
        )
        return AnalysisReport.model_validate(result, from_attributes=True)

    try:
        outcome = await deps.registry.call(
            ModelRole.AGENT, feature="analysis.report", run=run, prompt_text=prompt
        )
    except AppError as exc:
        logger.warning("agent report failed", extra={"error": type(exc).__name__})
        return {
            "report": None,
            "partial": True,
            "stop_reason": state.get("stop_reason")
            or f"the report could not be written ({type(exc).__name__})",
            "trace": ["report:unavailable"],
        }

    written: AnalysisReport = outcome.value
    own_reference = state.get("ticket_reference", "")

    # Citation grounding, over BOTH the structured field and the prose.
    #
    # Validating only `related_references` turned out to check the wrong
    # surface: an early run came back with that field empty while the summary
    # text said "previously reported and resolved in OS-193, OS-201". The
    # guardrail passed with nothing to check, and the citations a reader
    # actually sees went unvalidated.
    #
    # So references are collected from the prose as well, and the union is
    # filtered against what the agent actually retrieved. That fixes the
    # under-filled field using validated data instead of asking the model
    # again, and it means every reference a human reads has been checked.
    claimed = list(written.related_references)
    claimed += references_in(written.summary)
    for step in written.suggested_next_steps:
        claimed += references_in(step)

    grounded: list[str] = []
    ungrounded: list[str] = []
    for reference in dict.fromkeys(claimed):
        if reference == own_reference:
            # Citing the ticket under analysis is noise, not evidence.
            continue
        (grounded if reference in allowed else ungrounded).append(reference)

    if ungrounded:
        logger.warning(
            "agent report cited unretrieved tickets",
            extra={
                "run_id": str(state.get("run_id")),
                "dropped": len(ungrounded),
                "references": ungrounded[:5],
            },
        )
    written = written.model_copy(update={"related_references": grounded})
    dropped = len(ungrounded)

    return {
        "report": written,
        "model": outcome.model,
        "estimated_cost": state.get("estimated_cost", 0.0) + outcome.estimated_cost,
        "trace": [f"report:{outcome.model}:cited={len(grounded)}:dropped={dropped}"],
    }


# -------------------------------------------------------------------- routing


def route_after_decide(state: AnalysisState) -> str:
    """The limits live here, checked between turns.

    Order matters: budget first, then the model's own wish. A model that wants
    another turn does not get one when the budget is gone, and every exhausted
    path goes to `report` rather than `END` so the run still produces
    something.
    """
    decision = state.get("decision")
    if decision is None:
        return "report"
    return "act" if decision.action == "use_tool" else "report"


def route_after_act(state: AnalysisState, deps: AgentDeps) -> str:
    """Whether to take another turn.

    A closure over deps rather than reading settings, so a test can set
    `max_turns=1` and watch the cap fire.
    """
    turn = state.get("turn", 0)
    if turn >= deps.max_turns:
        return "report"
    if state.get("tool_calls", 0) >= deps.max_tool_calls:
        return "report"
    if state.get("estimated_cost", 0.0) >= deps.max_cost:
        return "report"
    if deps.clock.expired:
        return "report"
    return "decide"


def _limit_reason(state: AnalysisState, deps: AgentDeps) -> str:
    """Which limit fired, for the record.

    Reported to the user, so it says what happened in plain terms rather than
    naming a constant.
    """
    if state.get("turn", 0) >= deps.max_turns:
        return f"reached the {deps.max_turns}-turn limit"
    if state.get("tool_calls", 0) >= deps.max_tool_calls:
        return f"reached the {deps.max_tool_calls} tool-call limit"
    if state.get("estimated_cost", 0.0) >= deps.max_cost:
        return "reached the cost limit for one analysis"
    if deps.clock.expired:
        return f"ran out of time after {deps.clock.elapsed:.0f}s"
    return ""


async def check_limits(state: AnalysisState, deps: AgentDeps) -> dict[str, object]:
    """Record which limit stopped the run, if one did.

    A node rather than a side effect inside the router, because a router in
    LangGraph must be a pure function of state -- writing from it would be a
    silent state mutation the graph does not know about.
    """
    reason = _limit_reason(state, deps)
    if not reason:
        return {}
    logger.info(
        "agent run hit a limit",
        extra={"run_id": str(state.get("run_id")), "reason": reason},
    )
    return {"partial": True, "stop_reason": reason, "trace": [f"limit:{reason}"]}


# --------------------------------------------------------------------- prompts


def _decide_prompt(state: AnalysisState, deps: AgentDeps) -> str:
    lines = [
        f"TICKET TO ANALYSE: {state.get('ticket_reference', '?')}",
        "",
        "TOOLS AVAILABLE:",
    ]
    for tool in deps.tools.values():
        # Full descriptions, not just parameter names -- see `describe_tool`.
        lines.append(describe_tool(tool))

    observations = state.get("observations", [])
    lines += ["", f"STEPS TAKEN SO FAR: {len(observations)}"]
    if not observations:
        lines.append("(none yet — start by reading the ticket)")
    for index, observation in enumerate(observations, start=1):
        result = json.dumps(observation.get("result", {}), default=str)
        lines.append(
            f"\n[{index}] {observation.get('tool')}"
            f"({json.dumps(observation.get('arguments', {}), default=str)})"
            # Bounded per observation. Without this the prompt grows by the
            # full tool result every turn, and turn eight costs several times
            # turn one for information already summarised.
            f"\n    -> {result[:900]}"
        )

    remaining = deps.max_turns - state.get("turn", 0)
    lines += [
        "",
        f"You have {remaining} step(s) left. Decide the next action.",
    ]
    return "\n".join(lines)


def _report_prompt(state: AnalysisState, observations: list[dict[str, Any]]) -> str:
    allowed = sorted(set(state.get("seen_references", [])))
    lines = [
        f"Write the analysis for {state.get('ticket_reference', '?')}.",
        "",
        f"EVIDENCE GATHERED ({len(observations)} step(s)):",
    ]
    for index, observation in enumerate(observations, start=1):
        result = json.dumps(observation.get("result", {}), default=str)
        lines.append(f"\n[{index}] {observation.get('tool')}\n    -> {result[:900]}")

    lines += [
        "",
        "You may cite ONLY these ticket references, because they are the only ones you retrieved:",
        ", ".join(allowed) if allowed else "(none — do not cite any reference)",
    ]
    if state.get("stop_reason"):
        lines += [
            "",
            f"NOTE: this run {state['stop_reason']}. Write the best report the "
            "evidence supports and set confidence accordingly.",
        ]
    return "\n".join(lines)


def _references_in(result: dict[str, Any]) -> list[str]:
    """Ticket references present in a tool result.

    Collected from the *result*, not from the model's claims. That is what
    makes citation validation meaningful: the allowlist is built from what the
    tools actually returned.
    """
    found: list[str] = []

    def walk(value: Any) -> None:
        if isinstance(value, dict):
            reference = value.get("reference")
            if isinstance(reference, str) and reference.upper().startswith("OS-"):
                found.append(reference)
            for item in value.values():
                walk(item)
        elif isinstance(value, list):
            for item in value:
                walk(item)

    walk(result)
    return list(dict.fromkeys(found))


def _summarise_result(result: dict[str, Any]) -> str:
    """A one-line description of a tool result, for the step log.

    The step log is read by a human asking "why did it miss the obvious
    duplicate", so it needs the shape of each result, not the content.
    """
    if "error" in result:
        return f"error: {result['error']}"
    for key in ("returned", "total_matching", "total_tickets"):
        if key in result:
            return f"{key}={result[key]}"
    return f"keys={sorted(result)[:6]}"


# ----------------------------------------------------------------- the graph


@lru_cache(maxsize=1)
def analysis_graph() -> CompiledStateGraph[AnalysisState, AgentDeps, Any, Any]:
    """Build and compile the agent graph once."""
    graph: StateGraph[AnalysisState, AgentDeps, Any, Any] = StateGraph(
        AnalysisState, context_schema=AgentDeps
    )

    graph.add_node("prime", adapt(prime))
    graph.add_node("decide", adapt(decide))
    graph.add_node("act", adapt(act))
    graph.add_node("limits", adapt(check_limits))
    graph.add_node("report", adapt(report))

    # The ticket is fetched before the model is asked anything, so the first
    # decision is made with evidence rather than about how to get it.
    graph.add_edge(START, "prime")
    graph.add_edge("prime", "decide")
    graph.add_conditional_edges("decide", route_after_decide, {"act": "act", "report": "report"})

    # After acting, record any limit that fired, then decide whether to loop.
    # `limits` runs unconditionally so the reason is captured before routing --
    # the router cannot write state, and a partial report with no stated reason
    # is the thing this whole design is trying to avoid.
    graph.add_edge("act", "limits")

    def _route(state: AnalysisState, runtime: Runtime[AgentDeps]) -> str:
        return route_after_act(state, runtime.context)

    graph.add_conditional_edges("limits", _route, {"decide": "decide", "report": "report"})
    graph.add_edge("report", END)

    return graph.compile(name="analysis")


async def run_analysis(state: AnalysisState, deps: AgentDeps) -> AnalysisState:
    """Execute one analysis run.

    `recursion_limit` is LangGraph's own backstop, counted in supersteps rather
    than turns. Set from the turn cap with room for the fixed nodes, so a
    genuine long run completes while a topology bug that creates an unintended
    loop still fails loudly. Two independent limits on the same cycle is
    deliberate: `max_turns` is the business rule, this is the safety net for
    the rule being wrong.
    """
    result = await analysis_graph().ainvoke(
        state, context=deps, config={"recursion_limit": deps.max_turns * 4 + 10}
    )
    return dict(result)  # type: ignore[return-value]
