"""The chatbot: a stateful conversation over the ticket history.

```
    START
      |
    guard                  bound, redact, screen the message  (no LLM)
      |
  [blocked?] -- yes --> respond (refusal)
      |
      no
      |
    decide  <──────────┐   one LLM call: answer, or call a tool
      |                │
  [action?]            │
      |     \\           │
   use_tool  answer    │
      |         \\       │
     act ───────┼──────┘
                |
            respond        one LLM call: the reply, grounded in observations
                |
               END
```

## What makes this different from the analysis agent

They share the tool layer and the ReAct shape, and they differ in the two ways
that matter:

**State lives in Postgres, not in one function call.** The analysis agent runs
once and writes a report. A conversation runs across turns, days apart, and has
to remember. That is the checkpointer's job: LangGraph loads the prior state by
thread id before the first node runs and saves it after the last, so "what
about the payroll one?" resolves against what was said ten minutes ago.

**History is bounded, and the bound is the interesting part.** `add_messages`
accumulates every turn, which is correct for a transcript and wrong for a
prompt: after twenty turns the prompt is mostly old conversation, every turn
costs more than the last, and eventually the context window ends the thread.
So the transcript is kept in full and the *prompt* is built from a window —
see `_history_window`.

## Why the reply is a separate LLM call from the decision

The same reason as the analysis agent: a run that stops for any reason still
has to say something. A turn that used its tool budget, or whose model went
unavailable mid-loop, reaches `respond` and answers with what it has. The
alternative — the decision node emitting the final text — means any interruption
produces silence.
"""

from __future__ import annotations

import json
import re
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from functools import lru_cache
from typing import Annotated, Any, TypedDict

from langgraph.graph import END, START, StateGraph
from langgraph.graph.message import add_messages
from langgraph.graph.state import CompiledStateGraph
from langgraph.runtime import Runtime

from app.core.errors import AppError
from app.core.logging import get_logger
from app.modules.intelligence.llm.registry import ModelRegistry, ModelRole
from app.modules.intelligence.ports import ChatModel
from app.modules.intelligence.prompts import CHAT_SYSTEM
from app.modules.intelligence.schemas.reports import ChatDecision
from app.modules.intelligence.security.input import guard_input
from app.modules.intelligence.tools.registry import Tool, describe_tool
from app.modules.intelligence.utils.graph import adapt
from app.modules.intelligence.utils.references import REFERENCE_PATTERN

logger = get_logger(__name__)

#: How many prior messages go into the prompt.
#:
#: The full transcript is kept -- it is the product -- but only a window is
#: sent to the model. Eight messages is roughly four exchanges, which covers
#: the pronoun and topic references people actually make ("that one", "the
#: payroll bug") while keeping the prompt a fixed size no matter how long the
#: thread runs.
#:
#: The alternative, summarising older turns with another LLM call, is a real
#: technique and the wrong one here: it costs a call per turn to compress a
#: conversation that is usually short, and a summary that drops the detail
#: someone is about to refer to is worse than not having it.
HISTORY_WINDOW = 8

#: Sentinel that tells the turn-scoped reducer to start fresh.
#:
#: Needed because `operator.add` cannot express "reset". Passing `[]` for a
#: field with an add reducer appends nothing -- it does not clear -- so the
#: first version's per-turn state silently accumulated for the life of the
#: thread. The comment said "reset each turn"; the code appended. By turn four
#: the prompt carried every observation from every earlier question and the
#: cost per turn was still climbing.
#:
#: A sentinel inside the list is the idiom LangGraph itself uses --
#: `add_messages` recognises `RemoveMessage` the same way -- and unlike a
#: marker object it survives being serialised into a checkpoint.
CLEAR = "__clear__"


def turn_scoped(left: list[Any], right: list[Any]) -> list[Any]:
    """Append within a turn; replace when the incoming list starts with CLEAR.

    Used for the fields that are working memory for *one question*:
    observations, call signatures, and the node trace. The transcript uses
    `add_messages` instead, because it genuinely is cumulative.
    """
    if right and right[0] == CLEAR:
        return list(right[1:])
    return [*left, *right]


#: Defined in `prompts/`, so it can be versioned and diffed
#: independently of the control flow that uses it.
SYSTEM = CHAT_SYSTEM


class ChatState(TypedDict, total=False):
    """State for one conversation thread.

    Persisted by the checkpointer between turns, so every field here is state
    that has to survive a restart.
    """

    tenant_id: uuid.UUID
    user_id: uuid.UUID
    conversation_id: uuid.UUID

    #: The transcript. `add_messages` is LangGraph's reducer for exactly this:
    #: it appends, and it merges updates to a message by id rather than
    #: duplicating it.
    messages: Annotated[list[Any], add_messages]

    #: This turn's working memory. Reset each turn -- observations from ten
    #: minutes ago are not evidence for the current question, and carrying them
    #: would grow the prompt without bound.
    observations: Annotated[list[dict[str, Any]], turn_scoped]
    tool_calls: int
    call_signatures: Annotated[list[str], turn_scoped]

    decision: ChatDecision | None
    answer: str
    blocked: bool
    blocked_reason: str
    model: str
    trace: Annotated[list[str], turn_scoped]


@dataclass(slots=True)
class ChatDeps:
    """What the chat graph needs, injected."""

    registry: ModelRegistry
    tools: dict[str, Tool]
    #: Cap on tool calls per *turn*, not per conversation. A question needing
    #: more than a few lookups is one the user should be asked to narrow.
    max_tool_calls: int = 4
    history_window: int = HISTORY_WINDOW
    #: Names the assistant may need to disambiguate, resolved per request.
    #: Empty is fine; it only improves the disambiguation prompt.
    known_people: tuple[str, ...] = field(default=())


Node = Callable[[ChatState, ChatDeps], Awaitable[dict[str, object]]]


async def guard_message(state: ChatState, deps: ChatDeps) -> dict[str, object]:
    """Screen the incoming message before any model sees it.

    Same guardrail as the similar-issues path, and for the same reasons: bound
    the length, redact anything that looks like personal data before it can
    leave the process, and record injection markers without blocking on them.

    A node rather than a helper, so the graph topology makes it unskippable.
    """
    message = _last_user_message(state)
    if not message:
        return {
            "blocked": True,
            "blocked_reason": "There was no question to answer.",
            "trace": [CLEAR, "guard:empty"],
        }

    guarded = guard_input(message, feature="chat")
    if guarded.blocked:
        return {
            "blocked": True,
            "blocked_reason": guarded.reason,
            "trace": ["guard:blocked"],
        }

    marks = []
    if guarded.redactions:
        marks.append(f"redacted={sum(guarded.redactions.values())}")
    if guarded.injection_markers:
        marks.append(f"injection={len(guarded.injection_markers)}")

    # The first node of every turn, and therefore where per-turn state is
    # cleared. CLEAR is what makes it a reset rather than an append -- see the
    # constant.
    return {
        "blocked": False,
        "tool_calls": 0,
        "observations": [CLEAR],
        "call_signatures": [CLEAR],
        "trace": [CLEAR, f"guard:ok{':' + ','.join(marks) if marks else ''}"],
    }


async def decide(state: ChatState, deps: ChatDeps) -> dict[str, object]:
    """One LLM call: answer now, or call a tool first."""
    prompt = _decide_prompt(state, deps)

    async def run(model: ChatModel) -> ChatDecision:
        result = await model.generate_structured(
            SYSTEM, [{"role": "user", "content": prompt}], ChatDecision
        )
        return ChatDecision.model_validate(result, from_attributes=True)

    try:
        outcome = await deps.registry.call(
            ModelRole.CHAT, feature="chat.decide", run=run, prompt_text=prompt
        )
    except AppError as exc:
        logger.warning("chat decide failed", extra={"error": type(exc).__name__})
        return {
            "decision": None,
            "trace": [f"decide:unavailable:{type(exc).__name__}"],
        }

    decision: ChatDecision = outcome.value
    return {
        "decision": decision,
        "model": outcome.model,
        "trace": [f"decide:{decision.action}:{decision.tool or '-'}"],
    }


async def act(state: ChatState, deps: ChatDeps) -> dict[str, object]:
    """Run one tool. Every failure becomes an observation, never an exception."""
    decision = state.get("decision")
    if decision is None or decision.tool is None:
        return {"trace": ["act:no-tool"]}

    tool = deps.tools.get(decision.tool)
    if tool is None:
        return {
            "observations": [
                {
                    "tool": decision.tool,
                    "error": "unknown_tool",
                    "available_tools": sorted(deps.tools),
                }
            ],
            "trace": ["act:unknown-tool"],
        }

    try:
        arguments = json.loads(decision.arguments_json or "{}")
        if not isinstance(arguments, dict):
            raise ValueError("arguments must be a JSON object")
    except (json.JSONDecodeError, ValueError) as exc:
        return {
            "observations": [
                {"tool": decision.tool, "error": "invalid_json", "detail": str(exc)[:200]}
            ],
            "trace": ["act:bad-json"],
        }

    signature = f"{decision.tool}:{json.dumps(arguments, sort_keys=True)}"
    if signature in state.get("call_signatures", []):
        return {
            "observations": [
                {
                    "tool": decision.tool,
                    "error": "repeated_call",
                    "detail": "You already made this call this turn. Answer with what you have.",
                }
            ],
            "trace": ["act:repeat"],
        }

    result = await tool.invoke(arguments)
    return {
        "observations": [{"tool": decision.tool, "arguments": arguments, "result": result}],
        "call_signatures": [signature],
        "tool_calls": state.get("tool_calls", 0) + 1,
        "trace": [f"act:{decision.tool}"],
    }


async def respond(state: ChatState, deps: ChatDeps) -> dict[str, object]:
    """One LLM call: the reply the user reads.

    Reached by every path, including a blocked message and an exhausted tool
    budget, so the turn always produces an answer. A blocked message skips the
    model entirely -- there is nothing to ask it.
    """
    if state.get("blocked"):
        reason = state.get("blocked_reason") or "I could not use that message."
        return {
            "answer": reason,
            "messages": [{"role": "assistant", "content": reason}],
            "trace": ["respond:blocked"],
        }

    prompt = _respond_prompt(state, deps)

    async def run(model: ChatModel) -> str:
        return await model.generate(SYSTEM, [{"role": "user", "content": prompt}])

    try:
        outcome = await deps.registry.call(
            ModelRole.CHAT, feature="chat.respond", run=run, prompt_text=prompt
        )
    except AppError as exc:
        logger.warning("chat respond failed", extra={"error": type(exc).__name__})
        fallback = (
            "I could not reach the language model just now. The ticket pages and search still work."
        )
        return {
            "answer": fallback,
            "messages": [{"role": "assistant", "content": fallback}],
            "trace": [f"respond:unavailable:{type(exc).__name__}"],
        }

    answer = str(outcome.value).strip()
    if not answer:
        # The model returned nothing usable -- an empty content list, or only
        # non-text blocks. Observed once: a long prompt came back with no text
        # block at all, and the user saw a blank reply, which reads as the
        # feature being broken rather than the model having nothing to say.
        # Never return silence.
        logger.warning(
            "model returned an empty reply",
            extra={"model": outcome.model, "observations": len(state.get("observations", []))},
        )
        answer = (
            "I could not put together an answer for that. Try asking it more "
            "narrowly -- naming a team, a client, or a ticket reference."
        )

    answer, ungrounded = _ground_citations(answer, state)
    if ungrounded:
        logger.warning(
            "chat reply cited tickets it never retrieved",
            extra={"model": outcome.model, "references": ungrounded[:5]},
        )

    return {
        "answer": answer,
        # Appended to the transcript through `add_messages`, which is what the
        # next turn reads as history.
        "messages": [{"role": "assistant", "content": answer}],
        "model": outcome.model,
        "trace": [
            f"respond:{outcome.model}" + (f":ungrounded={len(ungrounded)}" if ungrounded else "")
        ],
    }


def _grounded_references(state: ChatState) -> set[str]:
    """References the assistant is entitled to cite.

    Two sources, and both are needed:

    * **This turn's tool results.** The primary source -- anything looked up
      just now.
    * **References already in the transcript.** A turn that answers from
      context alone ("what was that ticket number again?") legitimately cites
      something retrieved three turns ago. Validating against this turn's
      observations only would strip a correct answer, which is a worse failure
      than the one being prevented.
    """
    allowed: set[str] = set()

    for observation in state.get("observations", []):
        allowed.update(
            REFERENCE_PATTERN.findall(json.dumps(observation.get("result", {}), default=str))
        )

    for message in state.get("messages", []):
        allowed.update(REFERENCE_PATTERN.findall(_content_of(message)))

    return allowed


def _ground_citations(answer: str, state: ChatState) -> tuple[str, list[str]]:
    """Mark ticket references the assistant did not actually retrieve.

    ## Why this exists

    The reranker and the analysis agent both validate their citations. The chat
    reply did not, and running the whole pipeline on the local model showed
    exactly what that costs::

        Q: How many tickets does the Payroll team have open?
        A: The Payroll team has 3 open tickets. (OS-1042, OS-1043, OS-1044)

    None of those tickets exist -- this workspace runs from OS-1 to OS-220. The
    count came from a real tool result; the references were invented to decorate
    it. A weaker model does this more often, but no model is immune, and a
    fabricated ticket number is uniquely corrosive: it is specific, it looks
    checkable, and the reader only finds out after clicking.

    Marked rather than deleted. Removing the token silently would leave a
    sentence that reads as though it cited something, and rewriting the whole
    answer would discard a count that was correct. `[unverified: OS-1042]` is
    ugly on purpose -- the reader needs to see that one part of the sentence is
    load-bearing and one is not.
    """
    cited = set(REFERENCE_PATTERN.findall(answer))
    if not cited:
        return answer, []

    allowed = _grounded_references(state)
    ungrounded = sorted(cited - allowed)
    if not ungrounded:
        return answer, []

    marked = answer
    for reference in ungrounded:
        marked = re.sub(rf"\b{re.escape(reference)}\b", f"[unverified: {reference}]", marked)

    marked += (
        "\n\nNote: the reference(s) marked unverified were not found in the data I "
        "looked up, so treat them with suspicion."
    )
    return marked, ungrounded


# -------------------------------------------------------------------- routing


def route_after_guard(state: ChatState) -> str:
    return "respond" if state.get("blocked") else "decide"


def route_after_decide(state: ChatState, deps: ChatDeps) -> str:
    """Whether to run a tool or answer.

    The tool budget is checked here rather than in the model's prompt, because
    a limit the model is merely *told* about is a limit it can ignore.
    """
    decision = state.get("decision")
    if decision is None:
        return "respond"
    if decision.action != "use_tool":
        return "respond"
    if state.get("tool_calls", 0) >= deps.max_tool_calls:
        logger.info("chat turn hit its tool budget", extra={"calls": state.get("tool_calls")})
        return "respond"
    return "act"


# --------------------------------------------------------------------- prompts


def _last_user_message(state: ChatState) -> str:
    """The message being answered.

    Read from the end of the transcript rather than passed separately, because
    the checkpointer restores `messages` and a separate field would have to be
    kept consistent with it across restarts.
    """
    for message in reversed(state.get("messages", [])):
        role = _role_of(message)
        if role in {"user", "human"}:
            return _content_of(message)
    return ""


def _role_of(message: Any) -> str:
    if isinstance(message, dict):
        return str(message.get("role", ""))
    # LangChain message objects expose `type`, with "human"/"ai" rather than
    # "user"/"assistant". Both shapes appear because a checkpointed message
    # comes back as an object while a freshly appended one is a dict.
    return str(getattr(message, "type", ""))


def _content_of(message: Any) -> str:
    if isinstance(message, dict):
        return str(message.get("content", ""))
    return str(getattr(message, "content", ""))


def _history_window(state: ChatState, deps: ChatDeps) -> list[dict[str, str]]:
    """The last few messages, oldest first, excluding the current question.

    Bounded so the prompt does not grow with the conversation. The current
    question is excluded because it is added separately with clearer framing --
    including it twice makes some models answer the older copy.
    """
    messages = state.get("messages", [])
    window = messages[-(deps.history_window + 1) : -1] if len(messages) > 1 else []
    return [
        {"role": _role_of(message), "content": _content_of(message)[:600]} for message in window
    ]


def _decide_prompt(state: ChatState, deps: ChatDeps) -> str:
    lines: list[str] = []

    history = _history_window(state, deps)
    if history:
        lines.append("CONVERSATION SO FAR (oldest first):")
        for message in history:
            speaker = "User" if message["role"] in {"user", "human"} else "You"
            lines.append(f"  {speaker}: {message['content']}")
        lines.append("")

    lines += [f"CURRENT QUESTION: {_last_user_message(state)}", "", "TOOLS:"]
    for tool in deps.tools.values():
        # Full descriptions, not just parameter names. See `describe_tool` --
        # sending only the names silently withheld the tool contract and the
        # model could not know which filters existed.
        lines.append(describe_tool(tool))

    observations = state.get("observations", [])
    if observations:
        lines += ["", "WHAT YOU HAVE LOOKED UP THIS TURN:"]
        for index, observation in enumerate(observations, start=1):
            result = json.dumps(observation.get("result", observation), default=str)
            lines.append(f"[{index}] {observation.get('tool')} -> {result[:700]}")

    remaining = deps.max_tool_calls - state.get("tool_calls", 0)
    lines += [
        "",
        f"You may call {max(0, remaining)} more tool(s) this turn.",
        "Decide: call a tool, or answer now.",
    ]
    return "\n".join(lines)


def _respond_prompt(state: ChatState, deps: ChatDeps) -> str:
    lines: list[str] = []

    history = _history_window(state, deps)
    if history:
        lines.append("CONVERSATION SO FAR (oldest first):")
        for message in history:
            speaker = "User" if message["role"] in {"user", "human"} else "You"
            lines.append(f"  {speaker}: {message['content']}")
        lines.append("")

    lines += [f"QUESTION: {_last_user_message(state)}", ""]

    observations = state.get("observations", [])
    if observations:
        lines.append("DATA YOU RETRIEVED (this is the only data you may use):")
        for index, observation in enumerate(observations, start=1):
            result = json.dumps(observation.get("result", observation), default=str)
            lines.append(f"[{index}] {observation.get('tool')} -> {result[:1200]}")
    else:
        lines.append(
            "You retrieved no data this turn. Answer from the conversation only, and if "
            "the question needs data you do not have, say so."
        )

    if deps.known_people:
        # Supplied so the model can name the actual candidates when a name is
        # ambiguous, rather than inventing plausible ones or picking one.
        lines += ["", f"PEOPLE IN THIS WORKSPACE: {', '.join(deps.known_people)}"]

    lines += [
        "",
        "Write the reply. Cite ticket references. Be brief. If the data does not answer "
        "the question, say that instead of guessing.",
    ]
    return "\n".join(lines)


# ----------------------------------------------------------------- the graph


@lru_cache(maxsize=1)
def chat_graph_uncompiled() -> StateGraph[ChatState, ChatDeps, Any, Any]:
    """The topology, without a checkpointer bound.

    Separated because the checkpointer is a *runtime* dependency that may be
    unavailable, and `compile()` binds it. Building the topology once and
    compiling per checkpointer keeps the expensive part cached while letting
    the persistence layer be chosen at startup.
    """
    graph: StateGraph[ChatState, ChatDeps, Any, Any] = StateGraph(
        ChatState, context_schema=ChatDeps
    )

    graph.add_node("guard", adapt(guard_message))
    graph.add_node("decide", adapt(decide))
    graph.add_node("act", adapt(act))
    graph.add_node("respond", adapt(respond))

    graph.add_edge(START, "guard")
    graph.add_conditional_edges(
        "guard", route_after_guard, {"decide": "decide", "respond": "respond"}
    )

    def _route(state: ChatState, runtime: Runtime[ChatDeps]) -> str:
        return route_after_decide(state, runtime.context)

    graph.add_conditional_edges("decide", _route, {"act": "act", "respond": "respond"})
    # THE CYCLE: a tool result goes back for another decision, so the model can
    # chain lookups within one turn. Bounded by `max_tool_calls`, checked in the
    # router rather than trusted to the model.
    graph.add_edge("act", "decide")
    graph.add_edge("respond", END)

    return graph


def build_chat_graph(checkpointer: Any) -> CompiledStateGraph[ChatState, ChatDeps, Any, Any]:
    """Compile the graph with a checkpointer.

    Not cached on the checkpointer, because there is exactly one per process
    and the caller holds it. Compilation is cheap next to the model call that
    follows.
    """
    return chat_graph_uncompiled().compile(checkpointer=checkpointer, name="chat")
