# 11 — Code Structure

## What it is

Where the AI code lives, how it is layered, and what keeps it swappable and
testable.

The AI layer is **one module** in the existing modular monolith. Its outward
shape matches every other module — `service.py` and `router.py` — but its
internals are layered, because it has genuinely more moving parts than
`clients/` does.

---

## The tree

```
app/modules/intelligence/
│
├─ ports.py                 ← interfaces. ZERO third-party imports
│     EmbeddingProvider, VectorStore, LexicalSearch,
│     TicketCatalogue, ChatModel
│
├─ adapters/                ← one file per vendor. The only place vendors appear
│   ├─ ollama_embeddings.py   local embeddings, dimension probed at startup
│   ├─ chroma_store.py        one collection per tenant
│   ├─ gemini_chat.py         hosted chat + structured output via LangChain
│   └─ ollama_chat.py         local chat, `think: false`, real token counts
│
├─ lexical_repository.py    ← Postgres FTS. Satisfies LexicalSearch; not a
│                             vendor, so not in adapters/
├─ catalogue.py             ← reads candidate text for the reranker
├─ checkpointer.py          ← conversation persistence, sync saver + threads
├─ registry.py              ← role → model, fallback CHAIN, budgets, deadlines
├─ tracing.py               ← LangSmith, off unless a key is set
│
├─ domain/                  ← framework-free. No SQLAlchemy, no vendors
│   ├─ types.py               Relation, RetrievalSource, FusionMode, candidates
│   ├─ deps.py                RetrievalDeps — ports plus numbers, nothing else
│   ├─ query.py               query building, reference + identifier extraction
│   ├─ fusion.py              RRF, the cascade, weights — all pure functions
│   └─ reports.py             every LLM output schema
│
├─ guardrails/
│   ├─ rules.py               PII patterns, injection markers — data, not logic
│   ├─ input.py               bound, redact, screen
│   ├─ output.py              citation grounding, confidence floor, authority
│   └─ budget.py              rate limit, wall clock
│
├─ nodes/                   ← THE reuse unit: state in, partial state out
│   ├─ exact.py               resolve_references, probe_identifiers  (no LLM)
│   ├─ embed.py               embed_query                            (no LLM)
│   ├─ retrieve.py            retrieve_semantic, retrieve_lexical    (no LLM)
│   ├─ fuse.py                fuse_candidates                        (no LLM)
│   ├─ hydrate.py             guard_query, hydrate_candidates        (no LLM)
│   ├─ grade.py               grade_candidates + route_after_grading (no LLM)
│   ├─ rewrite.py             rewrite_query                          (1 call)
│   └─ rerank.py              rerank_candidates                      (1 call)
│
├─ graphs/                  ← composition only, no logic
│   ├─ state.py               the TypedDicts and their reducers
│   ├─ similarity.py          corrective RAG, with the rewrite cycle
│   ├─ analysis.py            the ReAct agent, with every limit
│   └─ chat.py                the stateful conversation
│
├─ tools/                   ← read-only, bounded, tenant-scoped
│   ├─ schemas.py             validated arguments (text-to-FILTER, not SQL)
│   ├─ tickets.py             search, get, comments, history, find_people
│   ├─ analytics.py           the allowlisted aggregation tool
│   └─ registry.py            build_tools, sanitize_tool_output, describe_tool
│
├─ evals/
│   ├─ datasets.py            golden clusters + derived identifier queries
│   ├─ retrieval.py           recall@k, MRR, nDCG — no LLM, no cost
│   ├─ report.py              the ablation table and its conclusion
│   └─ redteam.py             adversarial cases, as data
│
├─ models.py   schemas.py   repository.py
├─ deps.py                  ← wiring: constructs adapters, binds the session
├─ embedding_service.py     ← the write path
├─ service.py               ← similar issues: the module's public door
├─ analysis_service.py      ← the agent: queue, execute, rate
├─ chat_service.py          ← conversations: own, answer, expire
├─ subscribers.py  router.py  chat_router.py  worker.py
```

**Deviations from the obvious layout, and why.**

`ports.py` is one file rather than a `ports/` package. Five Protocols do not
need five files, and the rule that matters — "zero third-party imports here" —
is easier to check on one.

`domain/deps.py` holds `RetrievalDeps` rather than `deps.py` doing it. If the
dataclass lived next to the wiring, every node importing it would transitively
import `chromadb`, and the CI grep for vendor imports in `nodes/` would pass
while the layering had already collapsed. The shape lives in `domain/`; the
construction lives in `deps.py`.

`Relation` is defined in `domain/types.py` and imported *by* `models.py`, not
the other way round. The persistence layer knows the domain vocabulary; the
domain layer stays free of SQLAlchemy.

Three services rather than one, because they are three shapes of work: an
in-request lookup, a queued job polled over a minute, and a stateful
conversation. One `service.py` holding all three would be a file organised by
package rather than by lifetime.

**Two deviations from the obvious layout, both deliberate:**

`ports.py` is one file rather than a `ports/` package. Four Protocols do not
need four files, and the rule that matters ("zero third-party imports here") is
easier to check on one.

`domain/deps.py` holds `RetrievalDeps` rather than `deps.py` doing it. If the
dataclass lived next to the wiring, every node importing it would transitively
import `chromadb`, and the CI grep for vendor imports in `nodes/` would pass
while the layering had already collapsed. The shape lives in `domain/`; the
construction lives in `deps.py`.


---

## The seven rules that make it reusable

### 1. Dependency direction points inward, always

```
router → service → graphs → nodes → ports
                                      ↑
                             adapters plug in here
```

Nothing in `domain/` or `nodes/` imports `chromadb`, `google.generativeai`,
`ollama`, or `langchain_*`. That is what makes swapping Chroma for pgvector, or
Gemini for OpenAI, a **one-file change**.

It is mechanically checkable, which is worth more than a diagram:

```bash
# in CI — must return nothing
grep -rE "chromadb|google\.|ollama|langchain_" app/modules/intelligence/nodes/ \
                                                app/modules/intelligence/domain/
```

A rule that enforces itself does not decay.

### 2. Nodes are the reuse unit

```python
async def grade_relevance(state: SimilarityState, *, deps: Deps) -> dict:
    kept = [c for c in state["candidates"] if c.rrf_score >= deps.floor]
    return {"graded": kept, "needs_rewrite": len(kept) < 3}
```

Returns a **partial** state dict; LangGraph merges it. Consequences:

- Unit-testable with a plain dict and a fake `deps` — no LangGraph, no network
- `pii_redaction` is written once and used by all three graphs
- `retrieve` + `fuse` are shared by the similarity graph, the agent's
  `search_similar_tickets` tool, **and** the chatbot

### 3. Ports encode the gotchas structurally

```python
class EmbeddingProvider(Protocol):
    dimension: int
    async def embed_query(self, text: str) -> list[float]: ...
    async def embed_documents(self, texts: list[str]) -> list[list[float]]: ...
```

**Two methods, not one** — because Qwen3-Embedding needs an instruction prefix
on queries but not documents. With separate methods you *cannot* accidentally
embed a query as a document, because no single function exists that would let
you. The interface prevents the bug rather than a comment warning about it.

The same idea shows up in the asymmetry between the two search ports. Every
`VectorStore` method takes `tenant_id` **first**, and none omits it — Chroma has
no row-level security, so isolation has to be a property of the signature.
`LexicalSearch` takes no tenant argument at all, because it is bound to a
tenant-scoped session and Postgres RLS enforces the boundary. Each store is
isolated by whatever mechanism that store actually has, and the interface says
which.

`LexicalSearch` also has **two** search methods, for the same class of reason:
`search()` is OR-semantics ranking, `search_exact()` is AND-semantics lookup.
Using the ranking one for an error code returns every error report in the
corpus — a wrong answer, not a weaker one — so the two are not one function with
a flag.

### 4. `registry.py` is the only file that knows a model name

```bash
grep -rn "gemini\|qwen3" app/modules/intelligence/ | grep -v "adapters/\|registry.py"
# must return nothing
```

### 5. Graph files are wiring diagrams

```python
g = StateGraph(SimilarityState)
g.add_node("guard", input_guard)
g.add_node("embed", embed_query)
g.add_node("grade", grade_relevance)
g.add_conditional_edges("grade", route_after_grading,
                        {"rewrite": "rewrite", "rerank": "rerank", "give_up": "output"})
```

If a graph file contains business logic, that logic belongs in a node. A graph
you cannot read in one screen has a node missing.

### 6. Tools contain no logic

An agent tool calls `TicketService.search(...)` and shapes the result. Nothing
more. The moment a tool has its own query logic, it bypasses `policies.py` — and
the entire safety argument collapses. This is a hard rule, not a style
preference.

### 7. `service.py` is the only door

Routers and other modules import `IntelligenceService` and nothing else. Same
rule as every module in this project. Nothing outside `intelligence/` ever
imports `graphs/` or `nodes/`.

---

## Data model

All tenant-scoped tables get `tenant_id`, an RLS policy in the same migration,
and an entry in `TENANT_SCOPED_TABLES` so `scripts/check_rls.py` covers them.
The AI layer introduces a new leak surface; it gets no exemption.

| Table | Purpose | Key detail |
|---|---|---|
| `ticket_vector_state` | Bookkeeping: which tickets are embedded, with which model and content hash | Makes reconciliation possible without reading all of Chroma |
| `ai_jobs` | The transactional outbox | Written in the same transaction as the ticket |
| `ai_suggestions` | Similar-issue results with accept/reject status | Kept **separate** from the ticket's real fields |
| `ai_analysis_runs` | One agent run: status, report, tokens, cost, turns | |
| `ai_analysis_steps` | One row per agent turn: tool, args, tokens, latency | The only way to debug a bad analysis |
| `ai_usage` | Per-call token and cost attribution | Turns "AI costs too much" into a query |
| `ai_eval_pairs` | The golden set | Grows from real accept/reject |
| `conversations` | Chat threads, owner, expiry | Private to creator, 30-day retention |
| `langgraph.*` | Checkpoints | **Owned by `PostgresSaver`, not Alembic** — see below |

### Two migration systems in one database

`PostgresSaver.setup()` creates and owns its own tables. It does not go through
Alembic, so Alembic's autogenerate will try to **drop** tables it did not create.

Fix: give it its own schema and exclude that schema from Alembic:

```python
# alembic/env.py
context.configure(
    include_schemas=True,
    include_name=lambda name, type_, parent: not (
        type_ == "schema" and name == "langgraph"
    ),
)
```

### Notice what is *not* in the data model

**No table stores ticket text for the AI layer.** Chroma holds vectors, ids, and
filter metadata. Postgres holds the tickets. Results are hydrated from Postgres.
Sensitive content lives in exactly one place — one store to back up, secure, and
satisfy a deletion request against.

---

## Testing

| Layer | How | Needs network? |
|---|---|---|
| `domain/`, `nodes/`, `guardrails/` | Plain unit tests | No |
| `graphs/` | Fake deps; assert **which path ran** | No |
| `adapters/` | Contract tests + one integration test each | Integration only |
| `evals/` | The Tier-1 harness *is* a test | No |
| `tools/` | Assert tenant scoping and result bounds | No |

Three fakes make it work:

```python
class FakeEmbeddingProvider:
    """Deterministic vectors derived from a hash of the text.

    Same text always gives the same vector, and different texts give
    different ones — enough for retrieval tests without a model.
    """
    dimension = 384

class FakeChatModel:
    """Canned responses keyed by a marker in the prompt."""

class InMemoryVectorStore:
    """Exact cosine search over a dict. Correct, just not fast."""
```

**The payoff: the entire graph test suite runs in CI with no Ollama, no Chroma,
and no API key.** That is both good engineering and a concrete answer to "how do
you test non-deterministic systems" — the non-determinism is confined to the
adapter boundary, so everything above it is ordinary testable code.

### The tests that matter most

```python
def test_agent_has_no_write_tools():
    """The guarantee every other guardrail depends on."""
    for tool in build_tools(fake_db, fake_ctx):
        assert tool.name in READ_ONLY_TOOL_NAMES


def test_corrective_loop_fires_on_weak_retrieval():
    """Assert the PATH, not the output — that is what a graph test is for."""
    result = run_graph(weak_retrieval_fixture())
    assert "rewrite" in result["nodes_visited"]
    assert result["rewrites"] == 1


def test_tenant_cannot_reach_another_collection():
    """The structural isolation claim, tested."""
    ...
```

---

## Dependency isolation

The AI stack is heavy — LangChain, LangGraph, chromadb, RAGAS. It goes in an
optional group:

```toml
[project.optional-dependencies]
ai = ["langchain", "langgraph", "langchain-ollama", "langchain-chroma",
      "langchain-google-genai", "chromadb", "ragas", "langsmith"]
```

So `pip install -e .` gives a working application without any of it. That keeps
the "AI is never on the critical path" rule honest **at the dependency level**,
not just in code — and it keeps the core application's install fast and its
dependency surface small.

---

## Frontend

Mirrors the backend module name, as the existing convention does:

```
frontend/src/features/intelligence/
├─ api/          useSimilar, useAnalysis, useChat
├─ components/   SimilarIssuesCard, AnalysisReport, ChatPanel
└─ pages/        AnalysisPage, ChatPage
```

---

## Interview questions

**Q: How do you structure an LLM application so it is not tied to one provider?**

Ports and adapters. The interfaces — embedding provider, vector store, chat
model — are Protocols with no third-party imports at all. Each vendor gets one
adapter file, and a registry maps roles to configured models. Everything above
that layer talks to the interface, so swapping Chroma for pgvector or Gemini for
OpenAI is one new file plus a config line. I also enforce it mechanically: a CI
grep fails the build if any vendor package is imported inside the node or domain
layers. A rule that checks itself does not decay the way a convention does.

**Q: How do you test code that is inherently non-deterministic?**

By confining the non-determinism to the adapter boundary. The graph nodes are
plain functions of state, so they get ordinary unit tests with a dict in and a
dict out. For the graphs, I inject fake models returning canned responses and
assert **which path was taken** — did weak retrieval actually trigger the rewrite
cycle, did the turn cap actually stop the loop. That is fully deterministic. Then
a small number of contract tests exercise the real adapters. The result is that
the whole graph suite runs in CI with no Ollama, no Chroma, and no API key.

**Q: Why one module rather than separate services for RAG, the agent, and chat?**

They share the retrieval pipeline, the model registry, the job queue, the budget,
and the usage table. Splitting them would duplicate all five, and the agent's
most important tool *is* the similarity pipeline — so they would immediately need
to call each other across a network boundary for no benefit. It is one module in
a modular monolith, with services as the only cross-module interface, which
means the seam for extracting it later already exists if the scaling profile ever
diverges.

**Q: Your graphs are stateful and hit a database. How do you keep them testable?**

Dependency injection through a `deps` bundle passed to every node — session,
model registry, vector store, settings. Nodes never reach for a global or import
a client. So a test constructs a `deps` with fakes and calls the node directly.
For graphs, the checkpointer is swapped for an in-memory one, so state
persistence is exercised without Postgres. The database only appears in the
adapter and repository tests.

**Q: How do you handle two migration systems in one database?**

LangGraph's `PostgresSaver` creates its own tables and does not use Alembic, so
Alembic's autogenerate sees them as unknown and tries to drop them. The fix is
to give the checkpointer its own Postgres schema and exclude that schema from
Alembic's comparison. It is a small thing, but the failure mode is bad — a
migration that silently drops your conversation history — so it is worth being
deliberate about rather than discovering.

---

## Gotchas

- **The CI grep is the real enforcement.** Without it the layering decays within
  a month.
- **Nodes must return partial updates.** Returning the whole state couples a node
  to fields it does not own.
- **Do not import graphs or nodes from outside the module.** `service.py` is the
  door.
- **Exclude the `langgraph` schema from Alembic**, or a migration will try to drop
  your checkpoints.
- **Keep the AI dependencies optional.** It keeps the core install fast and the
  "never on the critical path" rule structural.
- **Register every new table** in `TENANT_SCOPED_TABLES` or the RLS gate will not
  cover it.
