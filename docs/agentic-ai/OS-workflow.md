# OS-Workflow — how a ticket becomes an answer

The end-to-end walkthrough of the AI layer, in the order things actually happen.
Every code block here is copied from the file named above it, not paraphrased.

**Companion page:** `rag-anatomy.html` in this folder is the same material laid out
visually. Open it in a browser.

---

## Contents

1. [The flow in one picture](#1-the-flow-in-one-picture)
2. [Where everything is stored](#2-where-everything-is-stored)
3. [Journey one — a ticket becomes a vector](#3-journey-one--a-ticket-becomes-a-vector)
4. [The embedding console](#4-the-embedding-console)
5. [Journey two — a vector becomes the panel](#5-journey-two--a-vector-becomes-the-panel)
6. [How ids become ticket details](#6-how-ids-become-ticket-details)
7. [The top-k funnel](#7-the-top-k-funnel)
8. [The reranker, step by step](#8-the-reranker-step-by-step)
9. [How we know the answer was good](#9-how-we-know-the-answer-was-good)
10. [Where the AI actually is](#10-where-the-ai-actually-is)
11. [Retrieval: why two searches](#11-retrieval-why-two-searches)
12. [The analysis agent](#12-the-analysis-agent)
13. [The chat assistant](#13-the-chat-assistant)
14. [Guardrails](#14-guardrails)
15. [Data shapes, end to end](#15-data-shapes-end-to-end)
16. [Patterns used](#16-patterns-used)

---

## 1. The flow in one picture

This is the workflow as it runs today, with automatic embedding switched **off**.

```
 ┌──────────────┐
 │ CS agent     │  raises a bug a customer reported
 │ fills form   │
 └──────┬───────┘
        │  POST /api/v1/tickets
        ▼
 ┌─────────────────────────────────────────────┐
 │  POSTGRES                                   │
 │    INSERT INTO tickets                      │   ← the source of truth
 │    (title, description, steps, team, …)     │
 └─────────────────────────────────────────────┘
        │
        │   nothing is embedded yet.
        │   AI_AUTO_EMBED_ON_WRITE = false
        ▼
 ┌─────────────────────────────────────────────┐
 │  EMBEDDING CONSOLE      /embeddings         │
 │  (team managers and tenant admins only)     │
 │                                             │
 │   OS-1   Full and final settlement…  ☐      │
 │   OS-2   Shift roster publish…       ☑      │
 │   OS-3   Employee custom fields…     ☑      │
 │                                             │
 │              [ Embed 2 ]                    │
 └──────┬──────────────────────────────────────┘
        │  POST /api/v1/ai/embeddings/embed
        ▼
 ┌─────────────────────────────────────────────┐
 │  for each selected ticket:                  │
 │    load  → chunk → hash → embed → upsert    │
 └──────┬──────────────────────────────────────┘
        │
        ▼
 ┌─────────────────────────────────────────────┐
 │  PINECONE   namespace = tenant_<uuid>       │
 │    id       = the ticket's UUID             │
 │    values   = 1024 floats                   │
 │    metadata = status, team, priority, …     │
 │    (NO ticket text)                         │
 └─────────────────────────────────────────────┘

        ─────────  later, a different request  ─────────

 ┌──────────────┐
 │ someone      │  opens OS-4
 │ opens a      │
 │ ticket       │
 └──────┬───────┘
        │  GET /api/v1/tickets/OS-4/similar
        ▼
 ┌─────────────────────────────────────────────┐
 │  OS-4's own title + description             │
 │            becomes THE QUERY                │
 └──────┬──────────────────────────────────────┘
        │  embedded live, right now
        ▼
 ┌─────────────────────────────────────────────┐
 │  PINECONE: "closest vectors to this one?"   │
 │    returns  ids + similarity scores ONLY    │
 │      01a07058-6833-…   0.889                │
 │      01a07058-682b-…   0.829                │
 └──────┬──────────────────────────────────────┘
        │
        ▼
 ┌─────────────────────────────────────────────┐
 │  POSTGRES                                   │
 │    SELECT * FROM tickets WHERE id IN (…)    │  ← details come from here
 └──────┬──────────────────────────────────────┘
        │
        ▼
 ┌─────────────────────────────────────────────┐
 │  SIMILAR ISSUES PANEL                       │
 │    OS-18   89% · very close   [open modal]  │
 │    OS-19   83% · very close                 │
 │    OS-3    44% · distant                    │
 └─────────────────────────────────────────────┘
```

**Two things people get wrong about this diagram**

- The ticket you open is **not** looked up in the vector database. It is the *query*.
  Its own stored vector only matters when *other* tickets are opened.
- Almost none of this is "AI". One embedding call, then cosine arithmetic and SQL.

---

## 2. Where everything is stored

| What | Where | Notes |
|---|---|---|
| Ticket text, status, team, client | **Postgres** `tickets` | The only copy. Row-level security scopes every read |
| Vectors | **Pinecone** | ids + 1024 floats + filter metadata. No text |
| "Has this been embedded?" | **Postgres** `ticket_vector_state` | model, dimension, `source_hash` |
| Queued embedding work | **Postgres** `ai_jobs` | The transactional outbox |
| What the model claimed | **Postgres** `ai_suggestions` | relation, confidence, and whether a human agreed |
| Agent runs and steps | **Postgres** `ai_analysis_runs` / `_steps` | The audit trail |
| Chat history | **Postgres** `ai_conversations` / `_messages` | Expired after 30 days |
| LangGraph checkpoints | **Postgres** `langgraph.*` | Its own schema, excluded from Alembic |

The rule: **Postgres is the truth, Pinecone is a derived index you can throw away.**
Proven, not asserted — the whole vector store was deleted and rebuilt from Postgres in
11.8 seconds.

---

## 3. Journey one — a ticket becomes a vector

### Shape at each step

```
Ticket row
   │  title + description + steps_to_reproduce
   ▼   chunking/policy.py
str                       1,115 chars, blank-line separated
   │
   ▼   embeddings/service.py
sha256                    "53be27d04d0d4c63…"   ← unchanged? stop
   │
   ▼   embeddings/embedder.py     POST localhost:11434/api/embed
list[float]               [0.02679, -0.03322, 0.00953, …]  1024 values
   │
   ▼   vectordb/pinecone_store.py
{id, values, metadata}
   │
   ▼
Pinecone namespace        tenant_01a05489439d716dbc78ab0867838221
```

### Step 1 — the ticket module emits an event

`app/modules/tickets/service.py`

```python
await emit(
    DomainEvent.TICKET_CREATED,
    self._session,          # the SAME session the ticket was written in
    self._tenant_id,
    ticket.id,
)
```

The `tickets` module does not import the AI layer and does not know it exists. It emits;
`intelligence` subscribed at startup. Dependencies point one way.

Passing **the emitter's own session** is what makes the next step atomic.

### Step 2 — the AI layer queues work

`app/modules/intelligence/subscribers.py`

```python
async def _enqueue_embed(ctx: EventContext) -> None:
    if not (settings.AI_ENABLED and settings.AI_SIMILARITY_ENABLED):
        return
    if not settings.AI_AUTO_EMBED_ON_WRITE:      # currently False
        return
    await AiJobRepository(ctx.session).enqueue(
        tenant_id=ctx.tenant_id,
        kind=AiJobKind.EMBED_TICKET,
        payload={"ticket_id": str(ctx.entity_id)},
    )
```

| | |
|---|---|
| **in** | `EventContext(session, tenant_id, entity_id)` |
| **out** | one `ai_jobs` row — or nothing, when the flag is off |

Because the handler writes through the emitter's session, the job row and the ticket
commit together or not at all. That is the **transactional outbox**: no broker, and no
dual write on the critical path.

> With `AI_AUTO_EMBED_ON_WRITE=false` this returns early. The console is then the only
> thing that creates vectors — which is the current setup.

### Step 3 — a worker claims the job

`app/modules/intelligence/repository.py`

```sql
SELECT id FROM ai_jobs
WHERE status = 'queued' AND scheduled_at <= now()
ORDER BY scheduled_at, created_at
LIMIT :limit
FOR UPDATE SKIP LOCKED          -- other workers step OVER locked rows
```

`SKIP LOCKED` is what lets you run five workers with no coordination. The claim commits
*before* slow work starts, so holding it never blocks anyone.

### Step 4 — load the ticket

`app/modules/intelligence/ingestion/ticket_source.py`

```python
def indexable_tickets() -> Select[tuple[Ticket]]:
    # ONE definition of "belongs in the index", so no caller can
    # forget the soft-delete filter.
    return select(Ticket).where(Ticket.deleted_at.is_(None))


async def load_ticket(session, ticket_id) -> Ticket | None:
    result = await session.execute(
        indexable_tickets().where(Ticket.id == ticket_id)
    )
    return result.scalar_one_or_none()
```

Returns `None` rather than raising — a ticket deleted between queueing and running is
ordinary, and failing the job would retry it five times against a row that will never
come back.

### Step 5 — assemble the text to embed

`app/modules/intelligence/chunking/policy.py`

```python
parts = [
    (ticket.title or "")[: settings.EMBED_TITLE_MAX],            # 300
    (ticket.description or "")[: settings.EMBED_DESCRIPTION_MAX],  # 2000
    (ticket.steps_to_reproduce or "")[: settings.EMBED_STEPS_MAX], # 1000
]
return FIELD_SEPARATOR.join(p.strip() for p in parts if p and p.strip())
```

**There is no chunker, and that is the decision.** Chunking exists to fit long documents
into a context window and to give retrieval a unit smaller than the whole document.
A ticket is 1–2 KB and is already the unit a human wants back — returning "paragraph
three of OS-1042" helps nobody.

What survives is bounding. Title first, so if truncation bites, the title is what
survives.

**Deliberately excluded:**

| Field | Why |
|---|---|
| comments | Change constantly, so the vector never settles |
| resolution notes | Written *after* the bug is understood. Including them leaks the outcome into a triage-time signal — inflates offline scores, performs worse in reality |
| client name | Would cluster tickets by customer instead of by problem. "Acme payroll bug" and "Acme leave bug" share "Acme" |

### Step 6 — hash, and usually stop

`app/modules/intelligence/embeddings/service.py`

```python
normalised = _WHITESPACE.sub(" ", text).strip().lower()
payload = f"{model}|{instruction_version}|{normalised}"
source_hash = hashlib.sha256(payload.encode("utf-8")).hexdigest()

existing = await self._state.get(ticket.id, self._embeddings.model_name)
if existing is not None and existing.source_hash == source_hash:
    return None            # already current — costs one indexed query
```

This is what makes the whole pipeline idempotent. A retry, a duplicate job, or a full
backfill over an already-embedded corpus each cost one cheap query.

Folding the **model name** into the hash is why switching
`qwen3-embedding` → `qwen3-embedding:0.6b` invalidated every stored vector
automatically. Nothing goes stale quietly, and a model swap is a config change rather
than a migration.

### Step 7 — the actual embedding call

`app/modules/intelligence/embeddings/embedder.py`

```python
async def _embed(self, inputs: list[str]) -> list[list[float]]:
    async with httpx.AsyncClient(timeout=httpx.Timeout(300.0)) as client:
        response = await client.post(
            f"{self._base_url}/api/embed",             # localhost:11434
            json={"model": self._model, "input": inputs},
        )
        response.raise_for_status()
        payload = response.json()

    vectors = payload.get("embeddings")
    if not vectors or len(vectors) != len(inputs):
        raise RuntimeError(
            f"Ollama returned {len(vectors or [])} embeddings for {len(inputs)} inputs"
        )
    return [[float(v) for v in vector] for vector in vectors]
```

Two public methods sit on top of this:

```python
async def embed_query(self, text: str) -> list[float]:
    vectors = await self._embed([f"{self._query_instruction}{text}"])
    return vectors[0]

async def embed_documents(self, texts: list[str]) -> list[list[float]]:
    prefixed = [f"{self._document_instruction}{t}" for t in texts]
    ...batched EMBED_BATCH_SIZE at a time...
```

**Why two methods and not one:** Qwen3-Embedding is *asymmetric* — a query takes a task
instruction prefix, a document does not. Using the wrong one produces **no error at
all**, just measurably worse recall. Separate methods make that impossible to get wrong
silently.

Model: `qwen3-embedding:0.6b`, **1024 dimensions**, probed from the model at startup
rather than configured.

### Step 8 — store

`app/modules/intelligence/vectordb/pinecone_store.py`

```python
namespace = namespace_for(tenant_id)     # "tenant_01a05489439d716d…"

vectors = [
    {
        "id": str(ticket_id),            # ← THE VECTOR ID *IS* THE TICKET ID
        "values": vector,                #   1024 floats
        "metadata": _clean_metadata(metadata),
    }
    for ticket_id, vector, metadata in items
]
self._require_index().upsert(vectors=vectors, namespace=namespace)
```

That one line is the answer to *"how does it know which ticket a vector belongs to?"*
It never has to work it out. The id **is** the ticket's UUID.

Metadata stored beside it (`ingestion/ticket_source.py`):

```python
{
    "ticket_number": ticket.ticket_number,
    "team_id":       str(ticket.team_id),
    "client_id":     str(ticket.client_id),
    "status":        ticket.status.value,
    "priority":      ticket.priority.value,
    "created_at":    ticket.created_at.isoformat(),
    "source_hash":   source_hash,
}
```

Chosen for **filtering**, not display. No ticket text — sensitive content lives in
exactly one place.

**Tenant isolation:** one namespace per tenant. A query names exactly one namespace and
cannot read across them, so there is no filter to forget. The name derives from the
authenticated tenant id and never accepts client input.

---

## 4. The embedding console

`/embeddings` — gated on the `embedding:manage` permission, held by team managers and
tenant admins. A CS agent can read every ticket and still cannot rebuild the index.

### Three states, and the middle one is the point

| State | Meaning |
|---|---|
| `not_embedded` | No vector exists |
| `embedded` | A vector exists **and** matches the current text |
| `stale` | A vector exists, but the text changed after it was written |

`stale` is why the page is a LEFT JOIN rather than a count. A stale ticket *looks*
indexed, so a naive "how many have vectors" query reports it healthy — while retrieval
returns it confidently for text that no longer exists. That is worse than no vector.

`app/modules/intelligence/embedding_console.py`

```python
query = (
    select(Ticket, TicketVectorState)
    .outerjoin(
        TicketVectorState,
        (TicketVectorState.ticket_id == Ticket.id)
        & (TicketVectorState.model == self._embeddings.model_name),
    )
    .where(Ticket.deleted_at.is_(None))
)
```

Then, per row:

```python
if vector_state is None:
    ticket_state = EmbeddingState.NOT_EMBEDDED
elif vector_state.source_hash == current_hash:
    ticket_state = EmbeddingState.EMBEDDED
else:
    ticket_state = EmbeddingState.STALE
```

### Why the embed runs synchronously

```python
MAX_SYNC_BATCH = 100
```

The automatic path queues jobs; the console does the work in the request and reports
what happened. The audience is the difference: **a person is watching**. A queued job
means the page says "submitted" and the operator has to go and find out whether it
worked.

The trade is bounded rather than ignored — over 100 tickets it refuses with a message
rather than silently truncating, because an operator who asked for 500 and got 100 will
believe the other 400 are done.

### Force re-embed

```python
if not force:
    existing = await self._state.get(ticket.id, self._embeddings.model_name)
    if existing is not None and existing.source_hash == source_hash:
        return None
```

Without `force`, embedding an already-current ticket is a no-op. With it, the hash check
is skipped — for a vector deleted directly in the store, a suspected bad write, or an
operator who simply wants the work redone.

---

## 5. Journey two — a vector becomes the panel

### Shape at each step

```
GET /tickets/OS-4/similar
   │
   ▼  api/similar_router.py → service.py
SimilarityState        {tenant_id, ticket_id, query_text, sources}
   │
   ▼  embeddings/embed_node.py
query_vector           [0.0268, -0.0332, …] 1024 floats   ← built LIVE
   │
   ├───────────────────────────┬───────────────────────────┐
   ▼ retrieve_semantic         ▼ retrieve_lexical
Pinecone query               Postgres ts_rank_cd
[(uuid, 0.889),              [(uuid, 0.412),
 (uuid, 0.829), …]            (uuid, 0.331), …]
   │                            │
   ▼ rank_only                  ▼ rank_only     ← SCORES DROPPED HERE
RankedList(SEMANTIC,         RankedList(LEXICAL,
  ticket_ids=(…))              ticket_ids=(…))
   └─────────────┬──────────────┘
                 ▼  retrieval/fuse.py
[FusedCandidate(ticket_id, score=1/(60+pos), ranks={…})]
                 │
                 ▼  service.py  _hydrate()      ← ids become details
SELECT * FROM tickets WHERE id IN (…)
                 │
                 ▼
[SimilarTicket(reference="OS-18", title=…, similarity=0.889)]
                 │
                 ▼  api/similar_router.py
JSON → React panel
```

### Step 1 — the ticket becomes the query

`app/modules/intelligence/service.py`

```python
state: SimilarityState = {
    "tenant_id":  self._tenant_id,
    "ticket_id":  ticket.id,        # excluded from its own results
    "query_text": build_query_text(ticket.title, ticket.description),
    "sources":    sources,          # {SEMANTIC, LEXICAL}
}
```

This is a document-to-document search dressed as a query-to-document one. The query is
built by the **same convention** as the stored embedding text — a query assembled
differently from the documents is the commonest reason a hybrid retriever quietly
underperforms.

### Step 2 — embed the query, live

`app/modules/intelligence/embeddings/embed_node.py`

```python
async def embed_query(state, deps) -> dict[str, object]:
    if RetrievalSource.SEMANTIC not in state.get("sources", frozenset()):
        return {"trace": ["embed:skipped"]}
    vector = await deps.embeddings.embed_query(state["query_text"])
    return {"query_vector": vector, "trace": ["embed"]}
```

**Nothing looks up OS-4's stored vector.** The ticket you are reading is the query; the
stored vectors are the corpus. That is why the panel works on a ticket created one
second ago that has never been embedded.

Note the return shape: a node returns a **partial dict**, and LangGraph merges it into
the state. No node mutates shared state.

### Step 3a — vector search

`app/modules/intelligence/vectordb/pinecone_store.py`

```python
self._require_index().query(
    vector=embedding,           # the 1024 floats from step 2
    top_k=fetch,                # limit + exclusions + 5
    namespace=namespace,        # hard tenant partition
    include_metadata=True,
    filter=where or None,
)

# …then for each match:
VectorHit(
    ticket_id=uuid.UUID(str(match["id"])),
    score=float(match.get("score", 0.0)),   # cosine SIMILARITY, no conversion
    metadata=dict(match.get("metadata") or {}),
)
```

> Pinecone returns **similarity** (larger is closer). Chroma returned **distance**
> (smaller is closer) and the old adapter converted with `1 - d`. Carrying that
> conversion across the port would have inverted every ranking while still producing
> plausible-looking numbers.

### Step 3b — keyword search, concurrently

`app/modules/intelligence/lexical/postgres_fts.py`

```python
query = text(_OR_TSQUERY).bindparams(query_text=query_text)
rank  = func.ts_rank_cd(_WEIGHTS, Ticket.search_vector, query)

stmt = (
    select(Ticket.id, rank.label("rank"))
    .where(
        Ticket.deleted_at.is_(None),
        Ticket.search_vector.op("@@")(query),
    )
    .order_by(rank.desc(), Ticket.id)
    .limit(limit + len(exclude or ()))
)
```

Where `_OR_TSQUERY` is the most important SQL in the module:

```sql
(
    SELECT string_agg(quote_literal(lexeme), ' | ')
    FROM unnest(tsvector_to_array(to_tsvector('english', :query_text))) AS lexeme
)::tsquery
```

Two properties, both load-bearing:

- **One tokenizer.** The lexemes come from the same analyzer that built `search_vector`,
  so query and index cannot disagree about what a token is.
- **OR, not AND.** `plainto_tsquery` joins with `&`, which is right for a search box and
  wrong here — the query is a whole bug description, so requiring every term matches
  nothing. Rank by *how much* overlaps rather than filter on total overlap.

Both retrievers run in the **same LangGraph superstep**, so they execute concurrently and
their writes merge through a reducer keyed by source.

### Step 4 — scores are deliberately discarded

`app/modules/intelligence/retrieval/fusion.py`

```python
def rank_only(hits, source) -> RankedList:
    """Turn scored hits into a ranked list, dropping the scores.

    The scores are dropped *here*, at the boundary, rather than being
    carried along and ignored later. If they were still in scope
    downstream, someone would eventually blend them.
    """
    seen, ordered = set(), []
    for ticket_id, _score in sorted(hits, key=lambda pair: -pair[1]):
        if ticket_id not in seen:
            seen.add(ticket_id)
            ordered.append(ticket_id)
    return RankedList(source=source, ticket_ids=tuple(ordered))
```

A cosine of 0.72 and a `ts_rank_cd` of 0.09 are not comparable numbers. "Third" and
"third" are. That is the entire reason rank-based fusion needs no weight tuning.

### Step 5 — fuse

```python
# leader first, in its own order, then everyone else in theirs
lead_first = [leader, *sorted(s for s in by_source if s != leader)]
for source in lead_first:
    for ticket_id in by_source[source].ticket_ids:
        if ticket_id not in seen:
            seen.add(ticket_id)
            ordered.append(ticket_id)

out += [
    FusedCandidate(
        ticket_id=ticket_id,
        score=1.0 / (k + position),     # rank-derived, k = 60
        ranks=dict(ranks[ticket_id]),   # {SEMANTIC: 1, LEXICAL: 4}
    )
    for position, ticket_id in enumerate(ordered, start=1)
]
```

`ranks` is what survives to the UI: a candidate found by both retrievers gets
`agreed = True`, the strongest confidence signal available without calling a model.

### Step 6 — grade (the corrective loop)

`app/modules/intelligence/retrieval/grade.py`

```python
if certain:                                        # a pinned exact match
    grade = Grade.GOOD
elif len(candidates) >= MIN_CANDIDATES and agreed:  # MIN_CANDIDATES = 3
    grade = Grade.GOOD
elif len(candidates) >= MIN_CANDIDATES:
    grade = Grade.GOOD
elif candidates and rewrites >= deps.max_rewrites:
    grade = Grade.GOOD                              # show what we have
elif rewrites < deps.max_rewrites:
    grade = Grade.WEAK                              # → rewrite, loop back
else:
    grade = Grade.EMPTY                             # → finish, say so
```

**No LLM here, deliberately.** Asking a model "are these results good?" costs a call and
a second of latency to answer a question two counts already answer.

---

## 6. How ids become ticket details

**This is the step most people ask about.** Pinecone returned three UUIDs and three
scores. Nothing else. Here is how that becomes a panel.

`app/modules/intelligence/service.py` — `_hydrate()`

```python
ids = [candidate.ticket_id for candidate in candidates]
rows = await self._session.execute(
    select(Ticket).where(Ticket.id.in_(ids), Ticket.deleted_at.is_(None))
)
by_id = {ticket.id: ticket for ticket in rows.scalars().all()}

for candidate in candidates:                 # fused order preserved
    ticket = by_id.get(candidate.ticket_id)
    if ticket is None:
        # In Pinecone but not in Postgres: deleted since the last reconcile.
        logger.warning("similar candidate missing from postgres", ...)
        continue
    out.append(SimilarTicket(
        reference=ticket.reference,          # "OS-" + ticket_number
        title=ticket.title,                  # ← FROM POSTGRES
        status=ticket.status.value,
        priority=ticket.priority.value,
        created_at=ticket.created_at,
        closed_at=ticket.closed_at,
        resolution_summary=_summarise(ticket.resolution_notes),
        score=candidate.score,               # fused rank score
        similarity=vector_scores.get(candidate.ticket_id),  # real cosine
        ranks=candidate.ranks,
        agreed=candidate.agreed,
    ))
```

**One SQL query turns three UUIDs into three full tickets.**

`reference` is not stored anywhere — it is a property on the model:

`app/modules/tickets/models.py`

```python
@property
def reference(self) -> str:
    return f"OS-{self.ticket_number}"
```

### Why details come from Postgres and not from vector metadata

- **One copy of sensitive data.** One store to secure, back up, and satisfy a deletion
  request against.
- **Always current.** Edit OS-18's title and the panel shows the new title immediately,
  *before* it is re-embedded — because the title is read fresh every time.
- **Drift is visible.** A candidate in Pinecone with no Postgres row is logged, not
  silently rendered as a broken card.

### Two scores, and only one is shown

| Field | Value | What it is | Shown? |
|---|---|---|---|
| `score` | ~0.0164 | Fused **rank** score, `1/(60+position)` | no |
| `similarity` | 0.889 | Raw **cosine** from Pinecone | **yes** — "89%" |

Fusion ranks by position, so every fused score lands near 0.0164 regardless of how alike
two tickets are:

```
OS-18   fused=0.0164   cosine=0.889   → 89% · very close
OS-19   fused=0.0161   cosine=0.829   → 83% · very close
OS-3    fused=0.0159   cosine=0.444   → 44% · distant
```

The fused column is nearly flat. Showing it as a "match score" would look like a
measurement and mean nothing — so the true cosine is carried through separately, as a
display-only field, and `RankedList` still discards scores for ranking purposes.

`similarity` is `null`, not `0`, when only keyword search found a ticket. "We did not
compare vectors" is a different claim from "the meanings are unrelated".

### Out as JSON

`app/modules/intelligence/api/similar_router.py`

```python
SimilarTicketOut(
    reference=item.reference,
    title=item.title,
    score=round(item.score, 6),
    similarity=(None if item.similarity is None
                else round(item.similarity, 4)),
    sources=[source.value for source in item.ranks],
    agreed=item.agreed,
    relation=item.relation.value if item.relation else None,
    confidence=item.confidence,
)
```

```json
{
  "reference": "OS-18",
  "title": "SCIM retry causes multiple employee profiles for the same Azure AD user",
  "status": "open",
  "score": 0.016393,
  "similarity": 0.8889,
  "sources": ["semantic", "lexical"],
  "agreed": true
}
```

> This router maps field by field — which is exactly how `similarity` was silently
> dropped the first time it was added. A field forgotten here defaults to `null` with no
> type error.

---

## 7. The top-k funnel

Every stage narrows the list. The numbers are not arbitrary — each one trades recall for
precision at a different price.

```
                    100 tickets in the corpus
                              │
              ┌───────────────┴───────────────┐
              ▼                               ▼
      semantic: top 50                lexical: top 50
      RETRIEVAL_SEMANTIC_LIMIT        RETRIEVAL_LEXICAL_LIMIT
              │                               │
              │   over-fetched: exclusions are removed AFTER
              │   fetch = limit + len(exclude) + 5
              └───────────────┬───────────────┘
                              ▼
                      fuse + de-duplicate
                              │
                              ▼
                     ≤ 20 candidates
                     RETRIEVAL_CANDIDATES
                              │
                              ▼
                   score floor 0.012
                   SIMILARITY_FLOOR
                              │
                              ▼
                  ≤ 10 sent to the reranker      ← cost lives here
                     MAX_CANDIDATES
                              │
                              ▼
              confidence floor 0.55  +  drop "unrelated"
              RERANK_CONFIDENCE_FLOOR
                              │
                              ▼
                       ≤ 5 shown
                       ?limit=5
```

### Where each number comes from

| Stage | Value | Why |
|---|---|---|
| Per-retriever depth | 50 | Over-fetch. Excluded ids (usually the ticket itself) are removed *after* the search and would otherwise eat into the limit |
| Fused candidates | 20 | The pool the reranker chooses from. **The pool's job is recall** |
| Score floor | 0.012 | Below this a candidate is not worth showing. A floor tuned to "always return something" is how this feature loses trust |
| Reranked | 10 | Each candidate is prompt tokens. Ten is where quality stops improving faster than cost |
| Confidence floor | 0.55 | A 0.3-confidence "related" is noise with a number attached |
| Displayed | 5 | Panel size. More than five and nobody reads them |

### The principle: recall early, precision late

```python
# retrieval/retrieve.py
hits = await deps.store.search(
    state["tenant_id"],
    vector,
    limit=deps.semantic_limit,          # 50
    exclude={own} if own is not None else None,
)
```

```python
# vectordb/pinecone_store.py
exclude = exclude or set()
# Over-fetch, because the excluded ids (usually the ticket itself) are
# removed after the search and would otherwise eat into the limit.
fetch = limit + len(exclude) + 5
```

The early stages maximise **recall** deliberately, because of one asymmetry:

> A reranker can reorder what it is given, but it cannot recover a document retrieval
> never returned.

So the candidate pool is generous and slightly noisy on purpose. Precision at the head is
the reranker's job, not retrieval's. This is also why the score floor is applied *after*
fusion rather than per retriever — a document ranked 40th by both retrievers is a better
candidate than one ranked 8th by a single retriever, and a per-retriever cutoff would
discard it before fusion ever saw the agreement.

### What the funnel looks like on real data

```
$ GET /tickets/OS-4/similar?limit=4

trace: guard:ok
       references:none
       identifiers:none
       embed
       retrieve:lexical:8          ← lexical found 8
       retrieve:semantic:9         ← semantic found 9
       fuse:9/17                   ← 17 raw hits → 9 unique above floor
       fuse:cascade:lead=semantic
       grade:good
       hydrate:9
       rerank:skipped              ← rerank off, fused order stands
       finish
```

`fuse:9/17` is the de-duplication: 17 hits across both retrievers, 9 distinct tickets
surviving the floor. With only 10 tickets in the corpus the funnel never fills; on a real
corpus you would see `fuse:20/100`.

---

## 8. The reranker, step by step

Optional and **off by default** (`AI_RERANK_ENABLED=false`). It costs one LLM call per
query, and the fused order is already a good answer.

```
 20 fused candidates
        │
        ▼  take the top 10                    MAX_CANDIDATES
 ┌──────────────────────────────────────────────────┐
 │ [hydrate]  load candidate TEXT from Postgres     │
 │            (never from vector metadata)          │
 └──────────────────────┬───────────────────────────┘
                        ▼
 ┌──────────────────────────────────────────────────┐
 │ build the prompt                                 │
 │   NEW REPORT: <the open ticket's text>           │
 │   EARLIER REPORTS:                               │
 │     [OS-18] (status: closed, closed 2026-03-04)  │
 │     title + first 600 chars                      │
 │     … × 10                                       │
 └──────────────────────┬───────────────────────────┘
                        ▼
 ┌──────────────────────────────────────────────────┐
 │ registry.call(RERANK)  → structured output       │
 │   gemini-3.5-flash-lite,  falls back to qwen3:8b │
 └──────────────────────┬───────────────────────────┘
                        ▼
   RerankResult { judgements: [
       { reference, relation, confidence, reason }, … ] }
                        │
                        ▼
 ┌──────────────────────────────────────────────────┐
 │ guard_output()                                   │
 │   1. grounded?   reference in the candidate set  │  → ungrounded++
 │   2. confident?  >= 0.55                         │  → below_floor++
 │   3. authority?  "I have closed this"            │  → rewrite reason
 └──────────────────────┬───────────────────────────┘
                        ▼
              drop relation == "unrelated"
                        │
                        ▼
        sort by (-confidence, severity of claim)
                        │
                        ▼
 ┌──────────────────────────────────────────────────┐
 │ _apply_judgements()                              │
 │   reorder candidates to the model's order        │
 │   DROP anything the model did not mention        │
 └──────────────────────┬───────────────────────────┘
                        ▼
                  top 5 → the panel
```

### The four relations

| Relation | Meaning | What a human does next |
|---|---|---|
| `duplicate` | The same defect, reported again | Link them; someone may already be working on it |
| `recurring` | The same defect as one already **resolved or closed** | Escalate — likely a regression |
| `related` | A different defect in the same area | Read for context |
| `unrelated` | Not useful | Filtered out before display |

`duplicate` vs `recurring` is the distinction that matters commercially, and it is why
the prompt carries ticket **status**:

`rerank/reranker.py`

```python
def _prompt(state, candidates, catalogue) -> str:
    """Build the comparison prompt.

    The candidate block carries **status and closure date** as well as text,
    because "recurring" is not a judgement about wording -- it is a judgement
    about a closed ticket coming back, and the model cannot make it without
    knowing the ticket was closed.
    """
    lines = [f"NEW REPORT:\n{state['query_text']}\n", "EARLIER REPORTS:"]
    for candidate in candidates:
        entry = catalogue.get(candidate.ticket_id)
        if entry is None:
            continue
        status = entry["status"]
        closed = f", closed {entry['closed_at'][:10]}" if entry.get("closed_at") else ""
        lines.append(
            f"\n[{entry['reference']}] (status: {status}{closed})\n"
            f"{entry['title']}\n{(entry.get('description') or '')[:600]}"
        )
    return "\n".join(lines)
```

### The call itself

```python
async def run(model: ChatModel) -> RerankResult:
    result = await model.generate_structured(
        SYSTEM, [{"role": "user", "content": prompt}], RerankResult
    )
    return RerankResult.model_validate(result, from_attributes=True)

try:
    outcome = await deps.registry.call(
        ModelRole.RERANK,
        feature="similar_issues.rerank",
        run=run,
        prompt_text=prompt,
    )
except AppError as exc:
    logger.warning("rerank unavailable", extra={"error": type(exc).__name__})
    return {"trace": [f"rerank:failed:{type(exc).__name__}"]}
```

Note the failure path: an unavailable model returns a trace entry, **not** an exception.
The fused order is still shown. That is correct degradation rather than a failure — the
fused results are what shipped before reranking existed, and they were measured to be
good.

### Filtering, in a deliberate order

`generation/grounded_answer.py`

```python
for judgement in judgements:
    if judgement.reference not in allowed_references:
        result.ungrounded += 1
        continue

    if judgement.confidence < confidence_floor:
        result.below_floor += 1
        continue

    claims = claims_authority(judgement.reason)
    if claims:
        # The model has described taking an action it cannot take. It has
        # no write tools, so nothing happened -- but a reader seeing "I
        # have closed this" believes it. Replaced rather than dropped,
        # because the classification itself may still be correct.
        result.authority_claims += 1
        judgement = judgement.model_copy(
            update={"reason": "Looks like the same problem area."}
        )

    result.judgements.append(judgement)
```

**Grounding is checked before confidence, and the order is the point.** A judgement citing
a ticket we never retrieved is not a weak answer to be filtered by confidence — it is
evidence something went wrong, and it is counted as that rather than absorbed into the
"low confidence" bucket.

The filtering lives in the output guardrail rather than being repeated in the reranker:

> Citation grounding and the confidence floor are safety properties, and a safety
> property implemented twice is a safety property that will eventually disagree with
> itself.

### Sorting

```python
kept = [j for j in guarded.judgements if j.relation not in DISCARDED]

# Highest confidence first, then by how strong the claim is, so a
# near-certain duplicate cannot sit below a confident "related".
kept.sort(key=lambda j: (-j.confidence, _severity(j.relation)))
```

### Applying the verdict

`service.py`

```python
def _apply_judgements(candidates, judgements, catalogue) -> list[FusedCandidate]:
    if not judgements:
        return candidates                    # the retrieval-only path

    rank_by_reference = {j.reference: index for index, j in enumerate(judgements)}
    judged = []
    for candidate in candidates:
        entry = catalogue.get(candidate.ticket_id)
        if entry is None:
            continue
        position = rank_by_reference.get(entry["reference"])
        if position is None:
            # The model dropped it. Trusting that is the point of reranking --
            # keeping it "just in case" would mean the confidence floor and the
            # unrelated verdict had no effect on what anyone sees.
            continue
        judged.append((position, candidate))

    judged.sort(key=lambda pair: pair[0])
    ...
```

This both **reorders and filters**. A candidate the model did not mention is gone — and
trusting that is what makes reranking worth its call.

### What gets logged, every time

```python
logger.info(
    "rerank complete",
    extra={
        "model": outcome.model,
        "judged": len(result.judgements),
        "kept": len(kept),
        "dropped_ungrounded": guarded.ungrounded,
        "dropped_low_confidence": guarded.below_floor,
        "rewritten_authority_claims": guarded.authority_claims,
        "latency_ms": outcome.latency_ms,
    },
)
```

`dropped_ungrounded` is the number to watch. A rising count means the model is inventing
ticket references, which is a prompt or model problem — not a retrieval one.

---

## 9. How we know the answer was good

Two loops, and they measure different things.

```
        ┌─────────────────── OFFLINE ───────────────────┐
        │  golden set → eval_retrieval → recall, MRR    │
        │  free, deterministic, runs on every change    │
        │  "did retrieval find the right ticket?"       │
        └───────────────────────────────────────────────┘

        ┌─────────────────── ONLINE ────────────────────┐
        │  panel → human clicks Agree / Not this        │
        │  ai_suggestions.status                        │
        │  "did anyone find that useful?"               │
        └───────────────────────────────────────────────┘
```

> Retrieval metrics say whether the right ticket was found; acceptance says whether
> anyone found that useful — **and those two have been known to move in opposite
> directions.**

### The online loop, in full

```
  panel renders
     OS-18   duplicate 0.91   "Same SCIM retry path"   [Agree] [Not this]
                                                          │        │
                                                          └────┬───┘
                                                               ▼
                              POST /tickets/suggestions/{id}/decide
                                          { "accepted": true }
                                                               ▼
                              ai_suggestions.status = accepted | rejected
                              ai_suggestions.decided_by  = user_id
                                                               ▼
                              acceptance_rate(days=30)
```

#### Step 1 — a judged result is persisted as a suggestion

`service.py` — `_persist_suggestions()`

```python
judged = [item for item in results if item.relation is not None]
if not judged:
    return
await self._suggestions.replace_similar(
    self._tenant_id,
    ticket.id,
    model,
    [
        {
            "related_ticket_id": item.ticket_id,
            "relation": item.relation,
            "confidence": item.confidence,
            "reason": item.reason,
            "payload": {
                "score": item.score,
                # The similarity that produced this suggestion, kept so
                # accepted/rejected pairs can later be analysed against
                # the score that generated them.
                "similarity": item.similarity,
                "sources": [source.value for source in item.ranks],
            },
        }
        for item in judged
    ],
)
```

Written in the request's transaction, so a suggestion shown to a user is a suggestion
that exists — the Agree button cannot 404 because the row was never committed.

Only **judged** results become suggestions. A retrieval-only hit is a search result, not
a claim, and there is nothing to agree or disagree with.

#### Step 2 — re-running does not discard human verdicts

`repository.py` — `replace_similar()`

```python
await self._session.execute(
    delete(AiSuggestion).where(
        AiSuggestion.ticket_id == ticket_id,
        AiSuggestion.kind == SuggestionKind.SIMILAR,
        AiSuggestion.status == SuggestionStatus.PENDING,   # ← only pending
    )
)
```

**Pending rows are deleted; decided rows are kept.** A human's accept or reject is data
paid for with someone's attention, and a re-run of the pipeline is not a reason to
discard it. It is also the only way the accuracy history survives a model change.

And a pair a human already rejected is never re-suggested:

```python
already_judged = {row for (row,) in decided.all()}

for item in items:
    related_id = item["related_ticket_id"]
    if related_id in already_judged:
        # Re-suggesting a pair a human already rejected is the fastest
        # way to teach people to ignore the panel.
        continue
```

#### Step 3 — the human clicks

`service.py`

```python
async def decide_suggestion(self, suggestion_id, *, accepted, user_id) -> AiSuggestion:
    """Record a human verdict on a suggestion.

    Accepting does **not** write ``tickets.duplicate_of_id``. Linking two
    tickets is a ticket-module action with its own permission and its own
    audit entry; this records agreement, which is a different fact.
    """
    suggestion = await self._suggestions.get(suggestion_id)
    if suggestion is None:
        raise NotFoundError("Suggestion not found.")
    decided = await self._suggestions.decide(
        suggestion, accepted=accepted, user_id=user_id
    )
    logger.info(
        "suggestion decided",
        extra={
            "suggestion_id": str(suggestion_id),
            "accepted": accepted,
            "relation": decided.relation.value if decided.relation else None,
            "confidence": decided.confidence,
            "model": decided.model,
        },
    )
    return decided
```

Note what is logged alongside the verdict: the **relation**, the **confidence**, and the
**model**. That is what makes "is the model well calibrated?" answerable later — you can
ask whether 0.9-confidence duplicates are accepted more often than 0.6-confidence ones.

#### Step 4 — the number that matters

`repository.py`

```python
async def acceptance_rate(self, tenant_id, days: int = 30) -> dict[str, int]:
    """Accepts, rejects, and pending over a window.

    The number that actually matters. Retrieval metrics say whether the
    right ticket was found; this says whether anyone found that useful --
    and those two have been known to move in opposite directions.
    """
    since = datetime.now(UTC) - timedelta(days=days)
    rows = await self._session.execute(
        select(AiSuggestion.status, func.count())
        .where(AiSuggestion.tenant_id == tenant_id, AiSuggestion.created_at >= since)
        .group_by(AiSuggestion.status)
    )
    return {status.value: count for status, count in rows.all()}
```

```json
{ "pending": 41, "accepted": 18, "rejected": 6 }
```

### The offline loop

Free, deterministic, and runs on every change:

```bash
python -m scripts.eval_retrieval    # recall@k, MRR, nDCG — zero LLM calls
python -m scripts.eval_rerank       # precision@5 — costs LLM calls
```

The retrieval harness runs **every configuration through the shipped code path**, selected
by a setting — an ablation with its own copy of the algorithm measures the copy, and the
two drift apart the first time one is edited.

Does the reranker earn its call?

| Configuration | precision@5 | results shown | s/query |
|---|---|---|---|
| retrieval only | 0.275 | 40 | 0.7 |
| retrieval + rerank | **0.635** | 20 | 4.3 |

**+0.360 precision@5 for +3.5s** — and it *halved* the list rather than padding it. A
panel showing two good matches beats one showing five of which three are noise.

### The honest caveat

The golden labels are planted by the seed and written by the same author as the system.
The eval script prints this itself:

```
note: labels are planted by the seed and written by the same author as the
      system, so read these numbers comparatively, not as production accuracy.
```

What the harness genuinely provides is the ability to tell whether a **change** helped, on
two query families, for free. Absolute accuracy needs the online loop — which is exactly
why the Agree / Not this buttons exist rather than the panel being read-only.

**Every click is a labelled pair for a future evaluation set.** That is the long-term
answer to "how do you know it is accurate": stop labelling by hand and start learning
from acceptance data.

---

## 10. Where the AI actually is

Counting honestly, for the similar-issues feature with reranking off:

| Step | AI? | What it really is |
|---|---|---|
| Building the query text | no | String concatenation |
| Embedding the query | **yes** | A local embedding model. Not a chat model, no reasoning |
| Vector search | no | Cosine similarity — arithmetic |
| Keyword search | no | A Postgres GIN index scan |
| Fusion | no | List manipulation |
| Grading | no | Two integer comparisons |
| Fetching details | no | `SELECT … WHERE id IN (…)` |
| Showing the score | no | The cosine number, formatted |
| **Rerank** | **yes** | An LLM labelling each result. **Currently off** |

**With reranking off, the entire feature makes zero chat-model calls.** One embedding
call, one vector search, one SQL query.

Cost by path:

| Path | LLM calls |
|---|---|
| Guarded input blocked | 0 |
| Good retrieval, rerank off | 0 |
| Good retrieval, rerank on | 1 |
| Weak retrieval → rewrite → rerank | up to 3 |

---

## 11. Retrieval: why two searches

### Each is good at what the other is bad at

| | Semantic (vector) | Lexical (keyword) |
|---|---|---|
| Finds | Paraphrases — *"salary slip shows no data for contract staff"* vs *"Payslip PDF generates blank for contractors"* | Literal tokens — `ERR-40001`, `80C`, `ECR`, `SAML_RESP_INVALID` |
| Weak at | Rare identifiers, flattened into "something about tax" | Wording that shares no words |
| Costs | An embedding call + a network hop | Nothing — the index already existed for the search box |

### Measured, on 100 tickets with no verbatim duplicates

| Configuration (unaided) | dup R@5 | dup R@20 | ident R@20 |
|---|---|---|---|
| semantic alone | **1.000** | **1.000** | 0.583 |
| lexical alone | 0.931 | 0.986 | **1.000** |
| RRF, equal weight | 0.986 | 1.000 | 0.792 |
| **cascade (shipped)** | **1.000** | **1.000** | 0.583 → **1.000** with probe |

Neither retriever wins everywhere, and *which one wins keeps changing with the query*.
That is the case for a hybrid — not that fusion beats its parts on average, but that it
never loses to the better part.

### Why not RRF

RRF's value is its **agreement bonus**: a document both retrievers ranked reasonably
beats one a single retriever loved. That bonus is also its failure mode when the
retrievers are of unequal quality for the query at hand:

```
1/(60+35) + 1/(60+2)  =  0.0266   >   1/(60+1)  =  0.0164
   weak retriever's rank 2               strong retriever's rank 1
```

And weighting the secondary down does not fix it — that is provable. Require that an
agreement bonus never lifts a document above the leader's own top hit:

```
1/(K + 2) + w/(K + 1)  ≤  1/(K + 1)
                   w   ≤  1/(K + 2)  =  0.016   at K = 60
```

At any weight small enough to be safe, the secondary contributes nothing but the
ordering of documents *below* the leader's list. "Keep the agreement bonus" and "never
rank worse than the better retriever" are not simultaneously satisfiable.

### Exact identifiers bypass ranking entirely

`ERR-40001` does not *suggest* which retriever is better — it **names a ticket**. So it
is looked up with AND semantics and pinned above the ranking, and only when it matches
exactly one ticket.

---

## 12. The analysis agent

On-demand, runs in the worker. Reads one ticket, gathers evidence, writes a triage
report.

```
 request → ai_jobs (ANALYSE_TICKET) → worker
                                        │
                                        ▼
                                     [prime]      ← ticket fetched by CODE
                                        │
                                        ▼
                      ┌──────────► [decide] ──────────┐
                      │                │              │
                      │                ▼              ▼
                      └────────────  [act]        [report]
                                   tool call          │
                                                      ▼
                                              ai_analysis_runs
```

### ReAct via structured output

Each turn the model returns an `AgentDecision` — a tool to call, or a decision to
finish. Deliberately **not** provider-native tool calling, so one port serves both Gemini
and Ollama identically. That is the only reason the fallback chain works.

### The seven tools, and the zero

| Tool | Returns |
|---|---|
| `get_ticket` | One ticket's full text and status |
| `search_tickets` | Filtered list — team, status, client, date |
| `find_similar_tickets` | The retrieval pipeline, reused |
| `get_history` | Status transitions — tells a fixed bug from a recurring one |
| `get_comments` | The conversation on a ticket |
| `count_tickets` | Aggregates, bucketed |
| `find_people` | Resolves a name to a user |

**There is no write tool.** Not a disabled one — none exists. A successful prompt
injection still cannot change anything.

### Four independent limits

| Limit | Value | Guards against |
|---|---|---|
| Turn cap | 8 | A model that will not stop reasoning |
| Wall clock | 300 s | A slow or hanging provider |
| Tool calls | 24 | Fan-out without converging |
| Cost cap | $0.05 | An expensive loop nobody is watching |

Plus repeat detection on tool-call signatures. **Every limit routes to `report`**, so a
partial answer is still written and persisted.

---

## 13. The chat assistant

Same tools, same read-only constraint, different shape: multi-turn, and the conversation
must survive a restart.

### Persistence

`AsyncPostgresSaver` cannot run on the Windows ProactorEventLoop. The solution is
`ThreadedPostgresSaver` — an async wrapper over the sync `PostgresSaver` using
`asyncio.to_thread`. It subclasses `BaseCheckpointSaver` because LangGraph
`isinstance`-checks the checkpointer rather than duck-typing it.

### Turn-scoped state

Observations from ten minutes ago are not evidence for the current question, and
carrying them grows the prompt without bound. But `operator.add` cannot express "reset" —
so a `CLEAR` sentinel plus a custom `turn_scoped` reducer does.

The transcript itself uses `add_messages`, which appends and merges updates by message id
rather than duplicating.

### Citation grounding

Every claimed reference is validated against **this turn's observations plus references
already in the transcript**. The second half matters — "what was that ticket number
again?" legitimately cites something retrieved three turns ago.

Ungrounded citations are marked `[unverified: OS-XXXX]` and a caveat line is appended.

---

## 14. Guardrails

Prompt wording is advisory — a determined injection talks a model past it. So enforcement
lives outside the model.

| Layer | File | Does |
|---|---|---|
| **Input** | `security/input.py` | Bound to 8000 chars, redact PII and credentials, log injection markers |
| **Tools** | `tools/registry.py` | No write verb exists. Structural, not behavioural |
| **Output** | `generation/grounded_answer.py` | Citation grounding, confidence floor, authority-claim rewriting |
| **Budget** | `security/budget.py` | Per-user rate, per-role daily, per-tenant monthly |

Injection markers are **logged, not blocking** — a bug report legitimately contains the
words "ignore previous instructions".

### Three rules that must not erode

1. **The model is never an authorization boundary.** RLS and permissions decide; the
   model only ever sees what the caller could already read.
2. **The agent has no write tools.** Not disabled — absent.
3. **Severity and impact stay human-entered.** A model could be talked into P1 by the bug
   reporter.

---

## 15. Data shapes, end to end

### Write

```
Ticket (ORM)
  ↓  build_embedding_text()
str                              1,115 chars
  ↓  compute_source_hash()
str                              64 hex chars
  ↓  embed_documents()
list[list[float]]                1 × 1024
  ↓  upsert()
{"id": str, "values": list[float], "metadata": dict}
```

### Read

```
Ticket (ORM)
  ↓  build_query_text()
str
  ↓  embed_query()
list[float]                      1024
  ↓  store.search()  /  lexical.search()
list[VectorHit]  /  list[LexicalHit]      (ticket_id, score)
  ↓  rank_only()
RankedList(source, ticket_ids)            order only
  ↓  cascade()
list[FusedCandidate]                      (ticket_id, score, ranks)
  ↓  _hydrate()   ← SELECT … WHERE id IN (…)
list[SimilarTicket]                       reference, title, similarity, …
  ↓  SimilarTicketOut
JSON
```

---

## 16. Patterns used

| Pattern | Where | What it buys |
|---|---|---|
| **Ports and adapters** | `ports.py` + adapters | Swapping Chroma for Pinecone took one new file and four config lines. No node, graph, or service changed |
| **Transactional outbox** | `ai_jobs` + worker | The job's existence is atomic with the ticket's. No broker |
| **Domain events** | `core/events.py` | Dependencies point one way: intelligence knows tickets, tickets knows nothing |
| **Corrective RAG** | grade → rewrite → embed | The pipeline grades its own retrieval, with a cap that is not the model's decision |
| **Hybrid retrieval** | semantic ∥ lexical | Neither retriever wins everywhere |
| **Cascade fusion** | `retrieval/fusion.py` | Never worse than the better retriever |
| **Content-hash idempotency** | `source_hash` | Re-runs are free; a model change invalidates the corpus automatically |
| **ReAct via structured output** | `AgentDecision` | One port serves hosted and local models, so fallback works |
| **Role-keyed model registry** | `llm/registry.py` | Swapping models is configuration. The evaluator deliberately has no fallback |
| **Structural over behavioural** | `tools/` | "No write tool exists" beats "the prompt says not to write" |

---

## Quick reference

```bash
# seed the demo corpus (10 detailed tickets)
python -m scripts.seed_demo --reset --profile ten

# the 100-ticket benchmark corpus instead
python -m scripts.seed_demo --reset

# check what is indexed vs what is not
python -m scripts.ai_backfill --status

# fix drift between Postgres and Pinecone
python -m scripts.ai_backfill --reconcile

# run the outbox worker (only needed when auto-embed is on)
python -m app.modules.intelligence.jobs.worker

# gates
python -m scripts.check_rls          # tenant isolation
python -m scripts.verify             # 71 end-to-end checks
python -m scripts.redteam            # 28 structural guardrail checks
python -m scripts.eval_retrieval     # retrieval ablation, zero LLM calls
```

### Current configuration

| Setting | Value |
|---|---|
| Embedding model | `qwen3-embedding:0.6b` |
| Dimensions | 1024, probed from the model |
| Vector store | Pinecone serverless, `os-tracker`, aws / us-east-1 |
| Tenant isolation | one namespace per tenant |
| Chat model | `gemini-3.5-flash-lite`, falling back to local `qwen3:8b` |
| Auto-embed on write | **off** — the console is the only path |
| Rerank | **off** by default — opt-in, costs one call per query |
| Retrieval depth | 50 per retriever, 20 candidates after fusion |
| Chat retention | 30 days |
