# 07 — The Bug Analysis Agent

## What it is

A button on a ticket. Pressing it runs an agent that investigates the ticket
across the whole history and writes an analysis.

It is the one genuinely agentic part of this system: a **ReAct loop** over
read-only tools, where the model decides what to look at next based on what it
just found.

---

## Why an agent here, and not for similarity

This is the distinction worth being able to defend.

**Similarity is not an agent, and should not be.** It fires on every ticket
open. It must be fast, cheap, and give the same answer twice. That is retrieval
plus one constrained call — an agent there buys latency and non-determinism and
returns nothing.

**Bug analysis genuinely is.** The work is:

> *"Find the similar tickets. Read their comments and how they were resolved.
> Check whether this client reported it before. Check whether this module is
> producing a cluster. Then tell me what is going on."*

That **cannot** be a single call, because you do not know in advance which
tickets to read. Which ones matter depends on what the first search returns;
which comments matter depends on what those tickets say. The model has to choose
its next step from what it just learned.

That is the definition of the problem an agent solves. Applying one anywhere
else in this system would be agentic theatre.

| | Similarity | Bug Analysis Agent |
|---|---|---|
| Trigger | Automatic | **On demand** |
| Latency | < 1 s | Minutes |
| Determinism | Required | Not expected |
| LLM calls | 0–1 | 8–12 |
| Cost | Fractions of a cent | Tens of cents |
| Output | Ranked list | Written report |

---

## What it produces

A structured report, every claim citing the tickets it came from:

| Section | Content |
|---|---|
| **Summary** | A plain restatement — useful when the original report is poorly written |
| **Prior occurrences** | Which tickets, and how each relates |
| **How it was fixed before** | Drawn from prior `resolution_notes` |
| **Regression assessment** | Was this closed and has it come back? |
| **Client impact** | Which clients have hit this, how often |
| **Cluster signal** | Is this module producing a run of related tickets? |
| **Suggested team** | With the reasoning |
| **Missing information** | The repro detail a developer will ask for anyway |
| **Confidence** | And its own caveats |

**Citations are not optional.** An analysis without references is unusable — a
developer needs to check the reasoning, not take it on faith.

The report is **a document a person reads**. It changes nothing about the ticket.

---

## The tool contract

> **Every tool is read-only. There is no write tool. There is nothing for the
> agent to do except look and report.**

| Tool | Returns |
|---|---|
| `search_similar_tickets(query, limit)` | The doc-04 hybrid pipeline, reused |
| `get_ticket(reference)` | One ticket's fields |
| `get_ticket_comments(reference)` | The conversation |
| `get_ticket_history(reference)` | Status transitions with timestamps |
| `search_tickets(filters)` | Structured search: team, client, status, dates |
| `get_client_ticket_summary(client_id)` | Counts and recent tickets for a client |
| `get_team_stats(team_id, window)` | Volume and reopen rate |
| `aggregate_tickets(dimension, metric)` | Allowlisted group-by, no SQL generation |

Four properties hold for all of them:

**1. They call services, not repositories.** Same entry point a route uses, so
every policy in `policies.py` applies unchanged. A tool that queried directly
would bypass authorization — and the whole safety argument with it.

**2. They are scoped to the invoking user's context.**

```python
ctx = RequestContext(user_id=run.requested_by_id, tenant_id=run.tenant_id, ...)
tools = build_tools(db, ctx)      # closures capture both
```

Every tool is a **closure over `(db, ctx)`**. The model supplies only arguments —
a reference, a filter — never a user, never a tenant. So the agent reads exactly
what the person who pressed the button could read.

The alternative, a service account with wide access, would make the agent a way
to read anything by asking it politely. There is no such account.

**3. They are tenant-scoped by RLS**, like everything else.

**4. They return bounded results.** Every tool caps its output — a limit with a
hard ceiling, truncated bodies. Without caps, one call can fill the context
window and the run dies mid-analysis.

### Tool design principle

**A few well-shaped tools, not many narrow ones.** `search_tickets` with a filter
object beats seven separate `search_by_team`, `search_by_client`,
`search_by_status` functions — fewer tools means less schema to reason about and
fewer wrong choices. This mirrors how the API itself is designed.

---

## Control loop

Agent runs are unbounded by nature. Four independent limits, because each alone
has a hole:

| Limit | Value | Stops |
|---|---|---|
| **Turn cap** | 12 | A tool loop that never converges |
| **Wall clock** | 5 minutes | A hung tool, rather than a looping model |
| **Cost cap** | Configurable, checked per turn | One run eating the monthly budget |
| **Task budget** | ~40,000 tokens | The model overspending — it *knows* the budget and paces itself |

The task budget is the interesting one where the provider supports it. It is not
`max_tokens`, which is a hard cut that truncates mid-sentence. A task budget
tells the model its ceiling so it prioritises and finishes gracefully. That is
the difference between *"the analysis stops halfway through a sentence"* and
*"the analysis covers the important parts and says what it skipped"*.

**Tripping any limit persists a partial report**, clearly marked partial. A
truncated analysis is more useful than a lost one.

---

## Execution model

An agent run takes minutes, so it cannot be request-response.

```
User presses "Analyse this bug"
        │
POST /api/v1/tickets/OS-193/analyse
        │
        ├── require(TICKET_READ)
        ├── budget check: this month's spend < tenant cap
        │
  ┌───────────── ONE transaction ─────────────┐
  │  INSERT ai_analysis_runs (status=queued)  │
  │  INSERT ai_jobs (kind='analyse_ticket')   │
  │  COMMIT                                   │
  └───────────────────────────────────────────┘
        │
   202 Accepted { run_id }
        │
   Worker claims the job (FOR UPDATE SKIP LOCKED)
        ├── opens a session scoped to the tenant
        ├── rebuilds the requester's RequestContext
        ├── runs the graph
        │     each turn: cost check, turn cap, redaction,
        │                append ai_analysis_steps row
        └── writes the report, status=completed
        │
   Frontend polls GET /api/v1/analysis-runs/{id}
```

Reusing the same outbox as embedding means a run is durable the same way: the
job's existence is atomic with the request, and a worker restart resumes rather
than losing it.

### `ai_analysis_steps` — worth the table

Recording each turn (which tool, what arguments, how many tokens, how long) is
**the only way to debug a bad analysis**. Without it, *"why did it miss the
obvious duplicate?"* is unanswerable. With it, you can see that it called
`search_similar_tickets` once with a poor query and never retried.

It is also how you notice waste — an agent calling the same tool five times with
near-identical arguments.

---

## Worked scenario

**OS-193: "Payslip PDF generates blank for contractors"**

```
TURN 1
  model → tool_use: search_similar_tickets("payslip blank contractors PDF")
  we run→ OS-214 (0.91), OS-201 (0.89), OS-108 (0.71), + 5 more
  steps → row: turn 1, search_similar_tickets, 8 results, 1.2s

TURN 2
  model → tool_use: get_ticket("OS-201")      ┐ one assistant message,
          tool_use: get_ticket("OS-214")      ┘ two parallel calls
  we run→ both concurrently, BOTH results returned in ONE user message
  steps → rows: turn 2, get_ticket ×2

TURN 3
  model → tool_use: get_ticket_comments("OS-201")
  we run→ 4 comments, including: "Fixed the contractor branch in the
          payslip renderer — it assumed a permanent employment record."

TURN 4
  model → tool_use: get_ticket_history("OS-201")
  we run→ created 2 Jun, resolved 10 Jun, closed 12 Jun,
          REOPENED 19 Jun, closed again 24 Jun
          ↑ the agent now knows this bug already came back once

TURN 5
  model → tool_use: get_client_ticket_summary(acme_id)
  we run→ Acme: 14 tickets, 6 payroll, including OS-201

TURN 6
  model → tool_use: aggregate_tickets(dimension="team", metric="reopen_rate")
  we run→ Payroll 18%, Workforce 11%, others < 8%
          ↑ cluster signal: Payroll reopens more than any other team

TURN 7
  model → final structured report
```

**The report:**

> **Summary.** Payslip generation returns an empty PDF for contractors. The
> renderer appears to assume a permanent employment record.
>
> **Prior occurrences.** OS-201 (closed 24 Jun) is the same bug — same failure,
> same client. OS-214 describes it in different words. Three tickets total.
>
> **How it was fixed before.** OS-201 was resolved by correcting the contractor
> branch in the payslip renderer, which assumed a permanent employment record.
>
> **Regression assessment.** **Likely a regression.** OS-201 was closed on 12
> June, reopened on 19 June, and closed again on 24 June. This is the third
> appearance. Recommend checking whether the OS-201 fix was reverted or is
> incomplete rather than treating this as new work.
>
> **Client impact.** Acme has raised this twice (OS-201, OS-193). Six of their
> fourteen tickets are payroll-related.
>
> **Cluster signal.** Payroll has an 18% reopen rate against a tracker average
> under 8%. This bug is part of a pattern, not an isolated defect.
>
> **Suggested team.** Payroll — consistent with OS-201 and OS-214.
>
> **Missing information.** No employee id or payroll period given. The
> developer will need one failing example to reproduce.
>
> **Confidence.** High on the duplicate link and the regression finding, both
> from status history. Medium on the root cause, which is inferred from a
> comment on OS-201 rather than from code.

**Why this is worth tens of cents:** no single query produces that. The
regression finding required reading OS-201's *history*, which the agent only
knew to fetch because a comment there mentioned a fix. That chain of decisions
is what an agent is for.

**A failing case.** A ticket titled *"not working"* with no description.
Retrieval returns noise, the agent tries two searches, finds nothing coherent,
and its report says so: *"Insufficient detail to analyse. No similar tickets
found. Recommend requesting reproduction steps."* Two turns, a few cents, and an
honest answer.

---

## Interview questions

**Q: When do you use an agent versus a fixed pipeline?**

When the next step depends on what the previous step returned. Our similarity
search is a fixed pipeline — embed, retrieve, fuse, rerank, always in that order
— so an agent there would add latency and non-determinism for nothing. Bug
analysis is an agent because you cannot know in advance which tickets to read;
that depends on what the first search finds, and which comments matter depends
on what those tickets say. If you can draw the flowchart up front, you do not
need an agent. Most things people build agents for are pipelines.

**Q: How do you keep an agent from doing something destructive?**

Structurally, not by prompting. Every tool it has is read-only — there is no
write tool in the tool set at all. So even a fully persuaded or hijacked agent
has no lever. Its output is a report a human reads, and if that report suggests
linking a duplicate, applying it is a separate authorized action through the
same policy layer a human click uses. Prompt instructions like "do not modify
data" are a hint, not a control; not having the capability is a control.

**Q: How does the agent respect permissions and multi-tenancy?**

Each tool is a closure over the invoking user's request context and a
tenant-scoped database session. The model supplies only arguments — a ticket
reference, a filter — never a user or tenant id. So the agent reads exactly what
the person who pressed the button could read, no more. There is deliberately no
service account with broader access, because that would make the agent a way to
read anything by asking it nicely. Row-level security still applies underneath,
so even a bug in a tool cannot cross a tenant boundary.

**Q: Agent runs are expensive and slow. How do you handle that?**

By making it on demand and asynchronous. It is a button, never automatic — at
tens of cents a run, firing it on every ticket creation would be both wasteful
and noticeable. The request writes a run row and an outbox job in one
transaction and returns 202 immediately; a worker executes it and the frontend
polls. Cost is bounded four ways: turn cap, wall clock, per-run cost cap checked
between turns, and a task budget the model itself is aware of. And prompt
caching on the tool schemas and system prompt roughly halves the cost, because
the accumulated history is resent every turn.

**Q: Why does the cost grow faster than the number of turns?**

Because the whole conversation is resent on every request. Turn 7 sends turns
1–6 plus every tool result they produced. So input tokens grow roughly
quadratically in turns, not linearly. That is why the turn cap is a cost control
as much as a safety one, why caching the stable prefix is the single biggest
lever, and why context editing — clearing tool results the model has already
consumed — becomes worth doing on longer runs.

**Q: How do you debug an agent that gave a bad answer?**

The step log. Every turn writes a row with the tool called, the arguments, token
counts, and latency. So "why did it miss the obvious duplicate?" becomes
answerable: you can see it searched once with a poor query and never retried, or
that it read three tickets but not their histories. Without that log an agent is
a black box and the only debugging tool is re-running it and hoping. Combined
with LangSmith traces you get both the structured record and the exact prompts.

**Q: How do you validate the agent's output?**

Three layers. Structured output, so the shape is guaranteed and the model cannot
return an action instead of a report. Citation validation — every ticket
reference in the report must appear in what the tools actually returned, and
anything else is dropped, so a hallucinated or injected reference never reaches
a user. And a thumbs up/down on the report, which is the only signal that tells
us whether the analyses are actually good enough to justify their cost.

---

## Gotchas

- **Parallel tool results must go back in one message.** Splitting them across
  messages trains the model to stop parallelising, and every later run is slower.
- **A tool that raises should return an error result**, not blow up the run — the
  agent can often recover and try something else.
- **Unbounded tool output kills runs.** One uncapped comment thread can fill the
  context window.
- **The step log is not optional.** Without it, a bad analysis is unexplainable.
- **Never let the agent write.** The moment one tool mutates, every other
  guardrail becomes best-effort.
- **Show progress.** A three-minute run with no feedback reads as broken. Stream
  the current node — "Reading OS-812" beats a spinner.
- **Keep previous runs.** Re-analysing after new comments is legitimate, and
  comparing runs is useful.
