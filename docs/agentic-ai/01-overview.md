# 01 — Overview

## What we are building

OS Tracker holds every bug the support team has ever raised. That history is
currently write-only: nobody reads it, because there is no way to find the one
ticket from four months ago that describes the bug in front of you.

Three capabilities change that.

| # | Capability | Trigger | Latency budget | LLM calls |
|---|---|---|---|---|
| 1 | **Similar Issues** | Automatic, on ticket open | ~1–3 s | 0 or 1 |
| 2 | **Bug Analysis Agent** | On demand, a button | Minutes | 8–12 |
| 3 | **Chatbot** | Conversational | Seconds per turn | 1–3 per turn |

---

## Capability 1 — Similar Issues

When someone opens `OS-193`, a card shows the tickets already raised that are
like it, each labelled with *how* it relates:

| Kind | Meaning | What the user does |
|---|---|---|
| **Duplicate** | Same bug, same root cause | Link it |
| **Related** | Same area, different bug | Context for the developer |
| **Recurring** | Fixed before, and it is back | Regression signal |

Three distinct questions hide inside "similar", and blurring them is the most
common reason this kind of feature gets ignored. A duplicate needs linking; a
recurring bug needs the earlier fix examined; a related one is just useful
background.

**Not on the create form.** Deliberate — it would add a live call to the most
important write path in the application, and the corpus is too small for it to
earn that yet.

## Capability 2 — Bug Analysis Agent

A button on the ticket. It answers:

> *"Find the similar tickets. Read their comments and how they were resolved.
> Check whether this client reported it before. Check whether this module is
> producing a cluster. Then tell me what is going on."*

Output is a written report with citations: prior occurrences, how it was fixed
before, whether this is a regression, which clients are affected, a suggested
team, and what repro detail is missing.

## Capability 3 — Chatbot

Multi-turn, with memory:

```
you:  was the payslip blank issue raised before?
bot:  Yes — OS-193 (in progress) and OS-201 (closed in June). OS-214
      describes the same failure in different words.
you:  what about for Acme specifically?
bot:  Acme raised OS-201. It was closed on 12 June after a fix to the
      contractor branch.
you:  how many P1 are open across all teams?
bot:  22 P1 tickets, 3 currently open. Workforce has the most.
```

Turn 2 resolves *"for Acme"* against turn 1 — that is what makes it stateful
rather than a series of unrelated searches.

---

## Why this stack

Each choice has its own document. The summary, with the reasoning compressed:

### Embeddings — Ollama + `qwen3-embedding` (local)

**Why local:** embedding means processing *every ticket ever written*. Ticket
text contains payroll figures and employee personal data belonging to our
customers' customers. A hosted embedding API would ship all of it out.

Local also means zero marginal cost, so re-embedding the entire corpus after a
model change costs CPU time rather than money — and that freedom matters,
because you *will* change the model.

**Alternative:** hosted embeddings (better quality, all data leaves).
**When to revisit:** if measured recall is unacceptable. Measure first — the
bottleneck is far more likely the rerank prompt than the embedding model.

→ [02-embeddings.md](02-embeddings.md)

### Vector store — Chroma, one collection per tenant

**Why Chroma:** it is the requested stack, it is simple to run locally, and it
is the most commonly named vector store in job descriptions.

**Why collection-per-tenant:** this is the important part. Postgres enforces
tenant isolation with row-level security, verified by a CI gate. **Chroma has no
equivalent.** A shared collection with a `where={"tenant_id": ...}` filter means
one forgotten filter is a cross-tenant leak with nothing underneath to catch it.
Collection-per-tenant makes isolation structural: the wrong collection returns
nothing rather than someone else's data.

**Honest cost:** two stores means the vectors can drift from the tickets. The
outbox pattern plus a reconciliation job handles it, but the problem is real and
did not exist when everything lived in one database.

→ [03-vector-store.md](03-vector-store.md)

### LLM — Gemini, with a local fallback

**Why Gemini:** free tier for development, paid for production.

**Why a fallback:** the free tier limits are severe — 20 requests *per day* on
2.5 Flash. An agent run makes 8–12 calls, so that is two runs a day. `qwen3:8b`
via Ollama takes over when quota is gone, so development is never blocked.

That constraint drives real design: a model registry keyed by *role* rather than
model name, aggressive response caching, and a token-bucket limiter.

→ [05-models-and-limits.md](05-models-and-limits.md)

### Orchestration — LangGraph

**Why not a plain chain:** the similarity pipeline needs a **cycle** — grade the
retrieved documents, and if they are weak, rewrite the query and retry. The
agent needs a **loop** with limits. The chatbot needs **durable state** across
turns. Chains are directed and acyclic; none of those three fit.

**Why not the provider SDK's own agent loop:** LangGraph gives checkpointing,
conditional edges, and a graph you can render — and it is what the job market
asks about.

→ [06-langgraph.md](06-langgraph.md)

---

## Architecture at a glance

```
                    FastAPI  (the existing modular monolith)
                              │
                    app/modules/intelligence/
                              │
        ┌─────────────────────┼─────────────────────┐
        │                     │                     │
   similarity graph     analysis agent         chat graph
   (deterministic)      (ReAct loop)        (checkpointed)
        │                     │                     │
        └─────────────────────┴─────────────────────┘
                              │
              ┌───────────────┼───────────────┐
              │               │               │
        PostgreSQL         Chroma          Ollama          Gemini
        tickets +          vectors,        embeddings      LLM
        checkpoints        per tenant      + fallback LLM
        (source of truth)
```

**PostgreSQL stays the single source of truth.** Chroma holds vectors, ids, and
minimal metadata — no ticket text. Results are hydrated from Postgres, so
sensitive content lives in exactly one place.

---

## Glossary

Terms used throughout, and worth being able to define precisely.

| Term | Meaning here |
|---|---|
| **RAG** | Retrieval-Augmented Generation — retrieve relevant documents, then have the model reason over *them* rather than its training data |
| **Embedding** | A fixed-length vector representing a piece of text, so that similar meanings sit close together |
| **Cosine distance** | The angle between two vectors; small angle = similar meaning. Chroma's `<=>` equivalent |
| **ANN / HNSW** | Approximate Nearest Neighbour search. Trades exactness for speed — you get *almost* the closest vectors, much faster |
| **Hybrid search** | Combining keyword (lexical) and vector (semantic) retrieval, because each catches what the other misses |
| **RRF** | Reciprocal Rank Fusion — merges two ranked lists by *rank* rather than score, so incomparable scores need no tuning. Implemented, measured, and **not shipped**: see doc 04 |
| **Cascade** | The combiner that did ship — the stronger retriever's order, with the other's finds appended below, so it can never rank worse than the better retriever |
| **Reranking** | A second, more expensive pass over a small candidate set to order it properly |
| **CRAG** | Corrective RAG — grade the retrieved documents, and if they are poor, rewrite the query and retrieve again |
| **ReAct** | Reason + Act. The agent alternates thinking and tool calls until it can answer |
| **Checkpointer** | LangGraph's persistence layer; saves graph state after each step so a conversation survives restarts |
| **Guardrail** | A check on input, tool output, or model output that constrains what the system can do or say |
| **Grounding** | Whether a claim is supported by retrieved evidence rather than invented |
| **Faithfulness** | RAGAS metric: does the answer only assert things the retrieved context supports? |

---

## Interview questions

**Q: Walk me through your RAG architecture.**

Hybrid retrieval into a reranker, with a corrective loop — plus one thing that
bypasses all of it.

First, the exact lookups: an explicit `OS-1042` reference or a literal
identifier like `ERR-40001` is *looked up*, not ranked, and pinned above
everything else. Facts get resolved; only estimates get ranked. That separation
took error-code queries from 0.417 recall to 1.000.

Then the ranking. A ticket's stored embedding drives a vector search in Chroma;
in parallel, Postgres full-text search runs over the same text. Both are
rank-based, because cosine similarity and `ts_rank_cd` are not comparable
numbers. I started with Reciprocal Rank Fusion and it measured *worse than
either retriever alone*, so the combiner is a cascade: the stronger retriever's
order with the other's finds appended below, which provably cannot rank worse
than the better retriever and still adds its recall. A grading node then
decides whether the candidates are good
enough — if not, an LLM rewrites the query and we retrieve again, up to twice.
Finally a reranker classifies each survivor as duplicate, related, or recurring
and explains why. The rerank is optional: with it disabled the pipeline still
returns fused results, which is both correct degradation and how we develop
without burning API quota.

**Q: Why not just use vector search? Why hybrid?**

Because bug reports are full of literal tokens that embeddings blur — `PF
challan`, `OS-1042`, `500 on /payroll/export`, a stack-trace line. Lexical
search matches those exactly. Meanwhile "payslip is blank" and "salary slip
shows no data" share almost no words and are the same bug, which is what
embeddings are for. Neither alone is sufficient. We measured both separately in
the eval harness before adding the reranker, so we know what each contributes.

**Q: How do you handle multi-tenancy in a vector database?**

One collection per tenant, not a metadata filter on a shared collection. The
application's primary datastore enforces isolation with Postgres row-level
security, which is verified by a CI gate. Chroma has no equivalent, so a
`where={"tenant_id": ...}` filter would be the *only* thing standing between
tenants — and one forgotten filter is a leak with nothing underneath to catch
it. Collection-per-tenant makes it structural: querying the wrong collection
returns nothing, rather than someone else's data.

**Q: What is the hardest part of this system?**

Not the graphs — the guardrails, specifically that tool results are untrusted
input. The obvious threat is injection in the initial prompt. But the agent
reads *ticket comments*, which are written by people outside the organisation.
So injection can arrive mid-loop, through a tool result, into a context that
started clean. The mitigation that actually holds is that the agent has no write
tools: even a fully persuaded agent has no lever. Everything else — data
envelopes, the non-spoofable operator channel, citation validation — reduces
noise around that.

**Q: How do you know it works?**

Three tiers. Retrieval metrics — recall@k, MRR, nDCG — against a golden set,
computed with no LLM at all, so they run on every change for free. RAGAS
faithfulness and relevancy on a small fixed sample, weekly, because a judge call
per metric per sample eats the API quota. And the real signal: suggestion
acceptance rate. The offline numbers grade the algorithm; acceptance grades the
product.

---

## Gotchas

- **Cold start is real.** Retrieval over 50 tickets returns noise. The synthetic
  dataset exists precisely so there are 100 tickets -- 80 distinct bugs and 20
  known duplicate pairs -- on day one.
- **"No similar issues found" must be a first-class answer.** A threshold set
  low enough to always return something makes the feature untrustworthy in week
  one and switched off in week two.
- **Two data stores can drift.** Postgres and Chroma need the outbox and a
  reconciliation job. This is the price of not using pgvector.
