# 06 — LangGraph

## What it is

LangGraph models an LLM workflow as a **state machine**: a typed state object, a
set of nodes that each take the state and return an update, and edges — some
fixed, some conditional — that decide what runs next.

Unlike a chain, a graph can **loop**, **branch**, and **persist**. All three are
required here.

---

## Why we chose this

### Why not a plain LangChain chain

A chain is a directed *acyclic* pipeline: A → B → C. Our three workflows each
break that in a different way.

| Workflow | What it needs | Why a chain cannot |
|---|---|---|
| Similarity | Grade results, and if weak, rewrite the query and retry | That is a **cycle** |
| Agent | Call tools repeatedly until it can answer | That is an unbounded **loop** |
| Chatbot | Remember previous turns across requests | That is **durable state** |

You can fake a loop with a `while` in Python around a chain, but you then own the
state passing, the step limits, the persistence, and the observability — which is
most of what LangGraph is.

### Why not the provider SDK's own agent loop

Anthropic and OpenAI both ship helpers that drive a tool loop. They are good, and
for a single-agent read-only loop they would be sufficient. LangGraph wins here
on three counts:

1. **Checkpointing.** The chatbot needs conversation state to survive a restart.
   That comes free with a checkpointer; hand-rolling it is a real chunk of work.
2. **Conditional edges as first-class structure.** The corrective-RAG cycle is
   declarative — you can render the graph and see the retry loop.
3. **It is provider-agnostic.** Swapping Gemini for anything else does not touch
   the graphs.

And, plainly: LangGraph is what the job market asks about, which is an explicit
goal of this project.

### Alternatives considered

| Option | Why not |
|---|---|
| LangChain LCEL chains | No cycles, no persistence |
| Hand-written `while` loop | Reimplements state, limits, checkpointing, tracing |
| Provider SDK tool runner | Fine for the agent, gives nothing for the chatbot's state |
| CrewAI / AutoGen | Multi-agent frameworks. We have one agent; their abstractions cost more than they return |

**When to revisit:** if the workflows collapse to single calls with no branching,
a chain would be simpler and honest.

---

## How it works technically

### The three core concepts

**State** — a `TypedDict` describing everything the workflow carries. Nodes
return *partial* updates, which LangGraph merges.

```python
class SimilarityState(TypedDict):
    tenant_id: str
    ticket_id: str | None
    query_text: str
    rewrites: int
    candidates: list[Candidate]
    graded: list[Candidate]
    results: list[SimilarResult]
    guardrail_flags: list[str]
    llm_used: bool
```

**Nodes** — functions of state. This is the reuse unit:

```python
async def grade_relevance(state: SimilarityState, *, deps: Deps) -> dict:
    kept = [c for c in state["candidates"] if c.rrf_score >= deps.floor]
    return {"graded": kept}          # a partial update, merged in
```

Returning a partial dict rather than the whole state is what keeps nodes
independent — a node declares only what it changes, so two nodes can be
reordered or reused without knowing each other's fields.

**Edges** — fixed (`add_edge`) or conditional (`add_conditional_edges`). Conditional
edges are what create branches and cycles:

```python
def route_after_grading(state: SimilarityState) -> str:
    if len(state["graded"]) >= 3:
        return "rerank"
    if state["rewrites"] < 2:
        return "rewrite"          # ← the cycle
    return "give_up"
```

### Reducers — the part that trips people up

By default a node's returned value **replaces** the state key. For a message
list that is wrong: turn 5 would wipe turns 1–4. Hence `add_messages`:

```python
class ChatState(TypedDict):
    messages: Annotated[list[AnyMessage], add_messages]   # appends
    intent: str                                            # replaces
```

`Annotated[..., add_messages]` tells LangGraph to *append* rather than replace,
and it also handles message-id deduplication. Getting this wrong is the single
most common LangGraph bug: the agent appears to forget everything each turn.

### Checkpointing — what makes the chatbot stateful

```python
checkpointer = AsyncPostgresSaver.from_conn_string(settings.DATABASE_URL)
graph = builder.compile(checkpointer=checkpointer)

await graph.ainvoke(
    {"messages": [HumanMessage("was the payslip bug raised before?")]},
    config={"configurable": {"thread_id": str(conversation_id)}},
)
```

State is written after **every node**, keyed by `thread_id`. Consequences:

- A follow-up request with the same `thread_id` resumes with full history — the
  client sends only the new message.
- A server restart mid-conversation loses nothing.
- You can inspect or replay any past state, which is genuinely useful for
  debugging a bad answer.

**Practical wrinkle:** `PostgresSaver` creates and owns its own tables via
`.setup()` — it does not go through Alembic. Two migration systems in one
database means Alembic's autogenerate will try to *drop* tables it did not
create. Fix: give it its own schema (`langgraph`) and exclude that schema from
Alembic.

### The three graphs

**1. Similarity — a cycle**

```
input_guard → embed → ┬→ retrieve_semantic ┬→ fuse → grade
                      └→ retrieve_lexical  ┘           │
                    ┌─────────────────────────────────┴──────┐
                 good                                      weak
                    │                                        │
                 rerank                                  rewrite ──┐
                    │                                   (max 2)    │
              output_guard ←── give_up ←────────────────────────────┘
                    │
                   END
```

**2. Agent — a loop**

```
input_guard → agent ⇄ tools → validate → persist → END
                ↑       │
                └── sanitize_tool_output
```

The `agent ⇄ tools` cycle continues while the model emits tool calls, bounded
by turn and cost limits enforced in a conditional edge.

**3. Chat — routing plus persistence**

```
input_guard → route_intent ─┬→ similarity_tool ──┐
                            ├→ structured_query ─┼→ answer → output_guard → END
                            └→ analytics ────────┘
```

Checkpointed, so `messages` accumulates across turns.

### Streaming

```python
async for event in graph.astream_events(input, config, version="v2"):
    ...
```

Two things surface to the UI: tokens as the answer is generated, and **which
node is currently running**. The second matters for the agent — a three-minute
run that shows nothing feels broken even when it is working. "Reading OS-812" is
far better than a spinner.

---

## Worked scenario

**A corrective-RAG cycle firing.** Someone opens a badly-written ticket titled
*"payroll issue"*.

```
node: input_guard
  → no PII, no injection markers, length ok
  → state: {guardrail_flags: []}

node: embed_query            (query_text = "payroll issue")
  → 4096-dim vector

node: retrieve_semantic + retrieve_lexical   (parallel)
  → semantic: 8 weak hits, top RRF 0.011
  → lexical:  2 hits on the word "payroll"

node: fuse
  → 9 candidates, best score 0.0164

node: grade_relevance
  → only 1 candidate above the floor
  → state: {graded: [1 item]}

conditional edge: route_after_grading
  → len(graded) = 1, which is < 3
  → rewrites = 0, which is < 2
  → GO TO "rewrite"                    ← the cycle

node: rewrite_query          (1 LLM call, cheap model)
  → "payroll calculation error salary processing failure deduction"
  → state: {query_text: <new>, rewrites: 1}

node: embed_query            (again, with the better query)
node: retrieve_*  → fuse
  → 14 candidates, best score 0.031

node: grade_relevance
  → 6 above the floor

conditional edge → "rerank"

node: rerank                 (1 LLM call)
  → 3 duplicates, 1 related, 2 dropped

node: output_guard
  → all returned ids present in the candidate set ✓
  → schema valid ✓
  → END
```

Total: 2 LLM calls, one of which only happened *because* the first retrieval was
poor. A chain would have reranked the 9 weak candidates and produced confident
nonsense.

**A chatbot thread across turns.**

```
Turn 1  thread_id = c-8f21
  messages: [Human("was the payslip bug raised before?")]
  route_intent → "similarity"
  similarity_tool → OS-193, OS-201, OS-214
  answer → "Yes — OS-193 (in progress), OS-201 (closed June) …"
  CHECKPOINT WRITTEN: 2 messages + retrieved refs

Turn 2  thread_id = c-8f21   (client sends ONLY the new message)
  LangGraph loads the checkpoint → messages now has 3
  route_intent sees the history → "structured_query"
  → resolves "for Acme" against turn 1's topic
  structured_query → {client: Acme, q: "payslip"}
  answer → "Acme raised OS-201, closed 12 June …"
  CHECKPOINT WRITTEN: 4 messages

Server restarts.

Turn 3  thread_id = c-8f21
  → checkpoint loads from Postgres, all 4 messages intact
  → "who fixed it?" resolves against OS-201
```

Turn 3 working after a restart is the whole point of a checkpointer.

---

## Interview questions

**Q: What is the difference between a LangChain chain and a LangGraph graph?**

A chain is a directed acyclic pipeline — each step feeds the next, and it always
runs the same path. A graph is a state machine: typed state, nodes that return
partial updates, and edges that can be conditional, so it can branch, loop, and
terminate early. You need a graph the moment the control flow depends on
intermediate results. Our similarity pipeline grades its own retrieval and
retries with a rewritten query if it was weak — that is a cycle, which a chain
cannot express. And a graph can persist state between invocations, which is what
makes a stateful chatbot possible.

**Q: What is a reducer in LangGraph and when do you need one?**

By default a node's return value replaces that key in the state. For accumulating
values that is wrong — a conversation's message list would be wiped on every
turn. A reducer tells LangGraph how to combine the old and new values instead;
`add_messages` appends and deduplicates by message id. This is the most common
source of "why does my agent have no memory" bugs: the state is technically
being updated correctly, it is just being overwritten each time.

**Q: How do you make an agent stateful across requests?**

A checkpointer plus a thread id. We use `AsyncPostgresSaver` against the database
the application already runs, so there is no new infrastructure. Graph state is
written after every node, keyed by `thread_id`, which for us is the conversation
id. A follow-up request sends only the new message; LangGraph loads the prior
state and the graph resumes with full history. It also survives a process
restart, and you can inspect or replay any past checkpoint — which is genuinely
useful when debugging why a conversation went wrong.

**Q: How do you stop an agent looping forever?**

Four independent limits, because any one alone has a hole. A turn cap in the
conditional edge that decides whether to call tools again. A wall-clock timeout,
for when a tool hangs rather than the model looping. A cost cap checked between
turns, since cost grows faster than turn count as history is resent. And where
the provider supports it, a task budget the model itself is aware of, so it
paces and finishes gracefully rather than being severed mid-sentence. Tripping
any of them persists a *partial* result — a truncated analysis is more useful
than a lost one.

**Q: How do you test a LangGraph graph?**

The nodes are plain functions of state, so most testing is ordinary unit
testing: pass a dict and a fake dependency bundle, assert the returned partial
update. For the graph itself, I point the model roles at a fake that returns
canned responses and assert **which path was taken** — did weak retrieval
actually trigger the rewrite cycle, did the turn cap actually stop the loop.
That is deterministic and needs no network or API key. The non-determinism is
confined to the adapter boundary, so everything above it is testable normally.

**Q: Why not CrewAI or AutoGen?**

They are multi-agent frameworks — their value is agents with distinct roles
negotiating with each other. We have one agent with a bounded read-only tool set,
so those abstractions would add concepts without solving a problem we have.
LangGraph is lower level, which here is the right level: I want explicit control
over the state shape, the limits, and where the guardrails sit, because the
guardrails are the hard part of this system. If we later needed several
specialised agents handing work off, that trade-off would change.

---

## Gotchas

- **Forgetting `add_messages`** is the classic bug. The agent silently loses all
  history each turn.
- **Nodes should return partial updates**, not the whole state. Returning
  everything makes nodes coupled to fields they do not own.
- **`PostgresSaver` owns its own tables** and does not use Alembic. Give it a
  separate schema or autogenerate will try to drop them.
- **Call `.setup()` once** on the checkpointer before first use, or the tables
  do not exist.
- **Conditional edge functions must be pure.** Doing I/O in a routing function
  makes the graph untestable and unpredictable.
- **A cycle with no exit condition is an infinite loop.** Always cap the retry
  counter *in the state*, not in a closure.
- **`astream_events` needs `version="v2"`.** The v1 event shape is different and
  the migration is silent.
