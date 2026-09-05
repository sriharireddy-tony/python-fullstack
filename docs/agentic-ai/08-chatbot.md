# 08 — The Chatbot

## What it is

A conversational interface over the ticket history. Multi-turn, with memory, so
follow-up questions resolve against what was already said.

```
you:  was the payslip blank issue raised before?
bot:  Yes — OS-193 (in progress) and OS-201 (closed 24 June). OS-214
      describes the same failure in different words.

you:  what about for Acme?
bot:  Acme raised OS-201, closed on 24 June after a fix to the contractor
      branch in the payslip renderer.

you:  how many P1 are open across all teams?
bot:  3 P1 tickets are currently open. Workforce has the most open work
      overall with 13 tickets.
```

Turn 2 has no subject of its own — *"what about for Acme?"* only means something
because turn 1 established the topic. That is what makes it a chatbot rather
than a search box.

---

## Why we chose this shape

### Why three tools rather than one

Questions about tickets come in three distinct shapes, and one tool cannot serve
all three:

| Question shape | Example | Needs |
|---|---|---|
| **Semantic** | "was this raised before?" | Vector + lexical retrieval |
| **Filtered list** | "open P1 tickets in Payroll" | Structured filters |
| **Aggregate** | "which team has the most open tickets?" | GROUP BY |

Trying to answer an aggregate question with a list tool means fetching every
matching row and counting client-side — six separate API calls to answer *"which
team has the most open tickets?"*, which we measured. Trying to answer a semantic
question with filters means keyword matching that misses paraphrase entirely.

So: **an intent router**, and three tools behind it.

### Why text-to-filter, never text-to-SQL

Text-to-SQL is the obvious approach and it is the wrong one here.

| | Text-to-SQL | Text-to-filter |
|---|---|---|
| Model produces | A SQL string | A validated Pydantic object |
| Injection surface | The entire query | None — fields are typed and allowlisted |
| Can it read other tables? | Yes, if it writes the join | No — the filter only reaches `TicketService.search` |
| Tenant isolation | Depends on the generated SQL | Enforced by the service and RLS |
| Bad output | Runs and returns wrong data, or errors cryptically | Fails schema validation |

The model emits:

```json
{"status": ["open"], "priority": ["p1"], "team": "Payroll"}
```

That is validated against a Pydantic model with typed, allowlisted fields, then
passed to the same `TicketService.search` the HTTP API uses. **The model never
composes a query** — it fills in a form.

The cost is expressiveness: anything the filter schema cannot express, the bot
cannot answer. That is an acceptable trade for removing an injection class
entirely, and the schema can be extended deliberately.

### Why a bounded aggregation tool

Confirmed against the real seeded data: *"which team has the most open
tickets?"* currently needs six separate calls and client-side counting. So:

```python
class AggregateQuery(BaseModel):
    dimension: Literal["team", "client", "status", "priority", "assignee", "month"]
    metric: Literal["count", "avg_resolution_hours", "reopen_rate"]
    filters: TicketFilters | None = None
```

**Allowlisted dimensions × allowlisted metrics.** The model picks from a fixed
menu, so there is still no generated SQL — the service maps the enum values to a
parameterised query. Same safety property, aggregate capability.

### Why conversations are private

A conversation is personal working notes. Making them readable by managers or
admins would change how people use the bot — they would stop asking the
half-formed questions that make it useful. So: private to the creator, not
visible to anyone else, expired after 30 days.

---

## How it works technically

### The graph

```
input_guard → route_intent ─┬→ similarity_tool ───┐
                            ├→ structured_query ──┼→ answer → output_guard → END
                            ├→ analytics ─────────┤
                            └→ general ───────────┘
```

Checkpointed with `AsyncPostgresSaver`, `thread_id` = conversation id.

### State

```python
class ChatState(TypedDict):
    messages: Annotated[list[AnyMessage], add_messages]   # appends
    tenant_id: str
    user_id: str
    intent: Literal["similarity", "structured_query", "analytics", "general"]
    retrieved: list[TicketRef]        # for citation validation
    guardrail_flags: list[str]
```

`add_messages` is load-bearing: without it, turn 2 replaces turn 1 and the bot
has no memory. That is the single most common failure in a stateful graph.

### Intent routing

One cheap LLM call classifying into four intents, **with the conversation
history in context** — which is what lets *"what about for Acme?"* be classified
as a structured query about the previous topic rather than gibberish.

```python
async def route_intent(state: ChatState, *, deps: Deps) -> dict:
    result = await deps.models.get("chat").with_structured_output(IntentDecision).ainvoke([
        SystemMessage(ROUTER_PROMPT),
        *state["messages"][-6:],        # bounded history, not the whole thread
    ])
    return {"intent": result.intent}
```

History is **bounded to the last six messages**. Unbounded, a long conversation
makes every routing call progressively more expensive for no gain — routing
needs recent context, not all of it.

### Reference resolution — and ambiguity

*"What is Divya working on?"* requires resolving a name to a user id. Real
organisations have two Divyas — we hit exactly that in our own seed data.

So the resolution tool **returns all matches**:

```
1 match  → proceed
0 matches → "I could not find anyone called Divya."
2+ matches → "Did you mean Divya Nair (Payroll) or Divya Menon (CoreHR)?"
```

Silently picking the first match is how a chatbot confidently returns the wrong
person's work. Asking is better than guessing, and the ambiguity is a permanent
property of names rather than a data-quality problem to fix.

### Answer generation and citation

The answer node receives only what the tools returned, and every ticket
reference in the output is validated against `state["retrieved"]`. A reference
the tools never produced is dropped before the user sees it.

### Streaming

`astream_events` surfaces tokens as they generate, and the current node name.
For a query that triggers retrieval, "Searching tickets…" then "Writing
answer…" is materially better than a three-second blank.

### Retention

```
Monthly job: delete conversations and their checkpoints older than 30 days.
```

Checkpoint tables grow quietly and contain ticket content. A retention job is
not optional — it is the only thing stopping unbounded accumulation of sensitive
text in a table nobody looks at.

---

## Worked scenario

**A four-turn conversation, with the mechanics shown.**

```
─── TURN 1 ─────────────────────────────────────────────────────
user: "was the payslip blank issue raised before?"

input_guard   → clean
route_intent  → "similarity"   (semantic phrasing, no filters named)
similarity    → OS-193 (0.94), OS-201 (0.89), OS-214 (0.85)
                state.retrieved = [OS-193, OS-201, OS-214]
answer        → "Yes — OS-193 is in progress, OS-201 was closed on
                 24 June, and OS-214 describes the same failure in
                 different words."
output_guard  → all 3 refs in state.retrieved ✓
CHECKPOINT    → 2 messages

─── TURN 2 ─────────────────────────────────────────────────────
user: "what about for Acme?"        ← no subject of its own

LangGraph loads the checkpoint. messages now has 3.
route_intent  → sees turn 1 in context → "structured_query"
structured_query → {"client": "Acme", "q": "payslip"}
                 → TicketService.search → OS-201
answer        → "Acme raised OS-201, closed 24 June after a fix to
                 the contractor branch in the payslip renderer."
CHECKPOINT    → 4 messages

─── TURN 3 ─────────────────────────────────────────────────────
user: "how many P1 are open across all teams?"

route_intent  → "analytics"   (asks for a count, not a list)
analytics     → AggregateQuery(dimension="team", metric="count",
                               filters={status:[open], priority:[p1]})
                → Workforce 1, CoreHR 1, Payroll 1, others 0
answer        → "3 P1 tickets are currently open — one each in
                 Workforce, CoreHR and Payroll."
CHECKPOINT    → 6 messages

─── server restarts here ───────────────────────────────────────

─── TURN 4 ─────────────────────────────────────────────────────
user: "who is working on the Payroll one?"

Checkpoint loads from Postgres — all 6 messages intact.
route_intent  → "structured_query"
                resolves "the Payroll one" against turn 3
structured_query → {"team": "Payroll", "priority": ["p1"],
                    "status": ["open","assigned","in_progress"]}
                 → OS-193, assignee Kavya Nair
answer        → "OS-193 is with Kavya Nair, currently in progress."
```

Turn 4 working after a restart is the whole point of the checkpointer. The
client sent only the new message each time.

**An ambiguous case.**

```
user: "what is Divya working on?"

resolve_user("Divya") → 2 matches
answer → "There are two people called Divya — Divya Nair in Payroll
          and Divya Menon in CoreHR. Which did you mean?"

user: "Nair"
→ resolves against the previous turn → OS-193, OS-214, and 2 more
```

**An out-of-scope case.**

```
user: "what is the capital of France?"

input_guard → scope_check fails: not about tickets
answer      → "I can only answer questions about the tickets in this
               tracker. Try asking about a bug, a team, or a client."
```

No tool call, no retrieval, no LLM generation beyond the guard. Cheap refusal.

---

## Interview questions

**Q: How do you make a chatbot stateful?**

A checkpointer plus a thread id. We use LangGraph's `AsyncPostgresSaver` against
the database the application already runs, with `thread_id` set to the
conversation id. Graph state is persisted after every node, so a follow-up
request sends only the new message and the graph resumes with full history. It
survives process restarts, and you can inspect past checkpoints when debugging.
The critical detail is the reducer — `add_messages` on the message list, so
turns accumulate instead of overwriting. Without it the bot silently has no
memory.

**Q: How does the bot answer "what about for Acme?" when that has no subject?**

The intent router sees the conversation history, not just the latest message. So
when it classifies turn 2, turn 1 — which established that we were discussing
the payslip bug — is in context. It classifies the turn as a structured query and
extracts both the new constraint (client = Acme) and the carried-over topic
(payslip). The history passed to the router is bounded to the last few messages,
because routing needs recent context and sending an entire long thread makes
every call progressively more expensive for no benefit.

**Q: Why not text-to-SQL? It would be far more flexible.**

Flexibility is exactly the problem. Generated SQL is an injection surface over
the whole database — and remember the model's input includes ticket text written
by people outside the organisation, so a bug report can contain instructions. If
the model composes queries, a persuaded model can compose a query that reads
another tenant's rows. Instead it fills in a validated Pydantic filter with
typed, allowlisted fields, which is passed to the same service method the HTTP
API uses. The model fills in a form; it does not write a query. The cost is that
anything the schema cannot express, the bot cannot answer — an acceptable trade
for removing a whole vulnerability class.

**Q: How do you handle aggregate questions like "which team has the most open
tickets"?**

A bounded aggregation tool: allowlisted dimensions — team, client, status,
priority, assignee, month — crossed with allowlisted metrics — count, average
resolution hours, reopen rate. The model picks two enum values and optional
filters; the service maps that to a parameterised GROUP BY. Same safety property
as the filter tool, no generated SQL. We added it because we measured the gap:
answering that question with the list endpoint took six separate calls and
client-side counting.

**Q: What happens when the user's question is ambiguous?**

The bot asks. Name resolution is the common case — real organisations have two
people with the same first name, and we hit that in our own seed data. So the
resolution tool returns *all* matches, and with more than one the bot asks which
was meant, carrying the disambiguation into the next turn. Silently picking the
first match is how a chatbot confidently returns the wrong person's work, and it
is worse than asking because the user has no way to notice it happened.

**Q: How do you stop the bot answering questions outside its remit?**

A scope check in the input guardrail, before any tool or generation. If the
question is not about tickets, teams, clients, or the tracker, it returns a
refusal that names what it *can* do. That is cheap — one guard evaluation, no
retrieval, no answer generation. It also prevents the bot being used as a general
assistant, which matters because it runs on a quota shared with features people
actually need.

**Q: The conversation contains ticket data. What about retention?**

Conversations and their checkpoints are deleted after 30 days by a scheduled
job, and conversations are private to their creator — not readable by managers
or admins. Both matter: the checkpoint tables grow quietly and contain ticket
content including payroll details, so unbounded retention is an accumulating
liability in a table nobody inspects. And privacy is what makes the bot useful —
if people knew their queries were readable, they would stop asking the
half-formed questions that make a conversational interface worth having.

---

## Gotchas

- **Forgetting `add_messages`** means no memory. The most common stateful-graph
  bug.
- **Bound the history sent to the router.** Unbounded, cost grows with
  conversation length for no benefit.
- **Never let the model produce SQL.** Filters and enums only.
- **Return all name matches**, not the first. Two Divyas is normal.
- **Validate references in the answer** against what the tools returned.
- **Checkpoint tables need a retention job** or they grow forever, holding ticket
  content.
- **Stream the node name.** "Searching tickets…" beats three seconds of nothing.
- **Cheap refusal for out-of-scope.** Guard first, do not spend a generation call
  discovering the question was about France.
