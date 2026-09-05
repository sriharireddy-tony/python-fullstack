# 03 — Vector Store

## What it is

A database built to answer one question fast: *given this vector, which stored
vectors are closest to it?* Ordinary indexes cannot do that — B-trees order
values on a line, and "close in 4096 dimensions" is not a range scan.

We use **Pinecone** — one serverless index, with **one namespace per tenant**.

> **This replaced Chroma.** The original build used Chroma with one collection
> per tenant. The port took one new file and four configuration lines, because
> the rest of the system talks to a `VectorStore` Protocol rather than to a
> client. That is the whole return on defining the port: swapping the vector
> database touched no node, no graph, and no service. The Chroma reasoning is
> kept below where it still applies, because the isolation argument is the same
> argument and only the mechanism changed.

---

## Why we chose this

### Why Pinecone

It is the requested stack, and it is the managed vector database named most
often in job descriptions. Practically: nothing to run locally, serverless
billing, and a free tier that covers a corpus this size.

What it costs compared to Chroma: a network hop on every search instead of a
local one, an API key to manage, and a hard dependency on a third party being
up. What it removes: a server to run, a volume to back up, and a process that
has to be started before the app works.

### Why one namespace per tenant — the important decision

This is the part to get right, because it is where the existing safety guarantee
does *not* carry over.

In Postgres, tenant isolation is enforced by **row-level security** with
`FORCE ROW LEVEL SECURITY`, verified end-to-end by 10 tests and a CI gate. A
developer who forgets a `WHERE tenant_id = ?` is caught by the database.

**No vector database has an equivalent.** Two options:

| Approach | Isolation rests on |
|---|---|
| Shared namespace + `filter={"tenant_id": ...}` | Every single query remembering the filter |
| **One namespace per tenant** | Which namespace you named |

A namespace is Pinecone's own multi-tenancy primitive: a query names exactly one
and cannot read across them. The namespace name is derived from the
authenticated tenant id and never accepts client input, so the caller cannot
influence which partition is read.

### Why not one index per tenant

Chroma collections are free, so collection-per-tenant cost nothing. Pinecone
indexes are provisioned, billable resources and the free tier caps how many
exist — so index-per-tenant would mean provisioning latency on every new tenant
and a hard ceiling on tenant count. Namespaces give the same guarantee without
either.

With a metadata filter, one forgotten `where` clause is a cross-tenant leak and
**nothing underneath catches it**. With collection-per-tenant, querying the
wrong collection returns *nothing* — the failure mode is an empty result, not
someone else's payroll data.

That asymmetry decides it. A bug that returns nothing gets noticed and fixed; a
bug that returns another tenant's data may never be noticed at all.

```python
def collection_name(tenant_id: uuid.UUID) -> str:
    return f"tickets_{tenant_id.hex}"
```

The tenant id comes from the request context, never from client input — same
rule as the Postgres session variable.

### Why no ticket text in the vector store

Pinecone *can* store the document in metadata alongside the vector. We store only the id and
minimal metadata, and hydrate the actual ticket from Postgres.

**Why:** sensitive text then lives in exactly one place. One store to back up,
one to secure, one to satisfy a deletion request against. A second copy of
payroll screenshots in a store with weaker access control is a liability with no
upside — we need the ids to look the tickets up anyway.

Cost: debugging is slightly harder, since you cannot read the matched text
directly in the vector store. Acceptable.

### Alternatives considered

| Option | Why not here |
|---|---|
| **pgvector** | Genuinely the better engineering choice — vectors inside the database that already enforces RLS, one store, transactional consistency. Not chosen because a managed vector database is the requested stack and the learning goal is explicit |
| Qdrant / Weaviate | Better at tens of millions of vectors; both add a service and a second access-control model for a corpus of thousands |
| Chroma | What this replaced. Local and free, but a server to run, a volume to back up, and a process that must be started before the app works |
| FAISS | A library, not a store. No persistence, no metadata filtering, no server |

**When to revisit:** if drift between Postgres and Pinecone becomes a recurring
operational problem, pgvector removes the entire class of issue.

---

## How it works technically

### Installing and configuring

```bash
pip install pinecone
```

Then in `backend/.env`:

```
PINECONE_API_KEY=pcsk_...
PINECONE_INDEX=os-tracker
PINECONE_CLOUD=aws
PINECONE_REGION=us-east-1
```

**Nothing to run.** This is the practical difference from Chroma, which needed
a server process started before the app worked — FastAPI and the outbox worker
are two separate processes, and embedded Chroma over one SQLite file is a
corruption risk, so it had to be `chroma run` in its own terminal. A managed
index removes that step and the class of "it works on my machine because my
server is up" problem with it.

**The index is created lazily and waited for.** `create_index` returns *before*
the index can serve traffic, so the first upsert against a fresh index fails
unless something waits for `status.ready`. The adapter does that waiting, and
also refuses to proceed if an existing index's dimension disagrees with what the
embedding model emits — loudly, once, rather than as a stream of confusing
per-vector errors.

### Index setup

```python
collection = client.get_or_create_collection(
    name=f"tickets_{tenant_id.hex}",
    metadata={
        "hnsw:space": "cosine",        # not the l2 default
        "embedding_model": model_name,
        "dimension": dimension,
    },
)
```

**The metric must be `cosine`, and it is fixed at index creation.** For text
embeddings, cosine is the right measure — it compares *direction*, which encodes
meaning, and ignores magnitude, which mostly encodes length. Leave it on L2 and
longer tickets drift apart from shorter ones on length alone.

### Writing a vector

```python
collection.upsert(
    ids=[str(ticket.id)],
    embeddings=[vector],
    metadatas=[{
        "ticket_number": ticket.ticket_number,
        "team_id": str(ticket.team_id),
        "client_id": str(ticket.client_id),
        "status": ticket.status.value,
        "created_at": ticket.created_at.isoformat(),
        "source_hash": source_hash,
    }],
    # no `documents` — text stays in Postgres
)
```

`upsert`, not `add`, so a re-embed after a ticket edit replaces cleanly rather
than raising or duplicating.

Metadata is chosen for **filtering**, not display: the status, team, and client
are there so a search can be narrowed *inside* Pinecone before results come back.

### Querying

```python
result = collection.query(
    query_embeddings=[query_vector],
    n_results=50,
    where={"status": {"$ne": "closed"}},     # optional pre-filter
    include=["distances", "metadatas"],
)
similarity = [1 - d for d in result["distances"][0]]   # cosine distance → score
```

Pinecone returns cosine **similarity**; larger is closer. Chroma returned **distance**, where smaller is closer, and the old adapter converted with `1 - d`. Carrying that conversion across the port would have inverted every ranking while still producing numbers that looked entirely plausible — which is why the direction is converted once, at the adapter boundary, and asserted in the adapter's own docstring.

### The sync problem — and the honest cost of two stores

The ticket lives in Postgres. The vector lives in Pinecone. Writing both is a
**dual write**, and dual writes fail in ways single writes do not:

| Failure | Result if handled naively |
|---|---|
| Write Pinecone, then Postgres rolls back | A vector for a ticket that does not exist |
| Commit Postgres, then the Pinecone write fails | A ticket with no vector, and **nothing records that it was missed** |

The transactional outbox solves it:

```
┌──────────── one Postgres transaction ────────────┐
│  INSERT tickets                                  │
│  INSERT ai_jobs (kind='embed_ticket')            │
│  COMMIT                                          │
└──────────────────────────────────────────────────┘
                    │
        worker claims the job, writes Pinecone,
        records ticket_vector_state, marks done
        (failure → retry with backoff)
```

The job's existence is atomic with the ticket's. Pinecone becomes **eventually**
consistent rather than possibly-never consistent.

### Reconciliation — because eventually consistent still drifts

A periodic job compares the two sides:

```
tickets in Postgres with no ticket_vector_state row    → enqueue embed
ticket_vector_state rows whose ticket is deleted       → delete from Pinecone
ids in Pinecone with no ticket_vector_state row        → orphan, delete
source_hash mismatch between the two                   → enqueue re-embed
```

`ticket_vector_state` in Postgres is the bookkeeping table that makes this
possible — without it, "which tickets are missing vectors?" requires reading
every id out of Pinecone and diffing, which does not scale and cannot be done
transactionally.

---

### The embedding model, and why 1024 dimensions

`qwen3-embedding:0.6b`, which emits **1024 dimensions natively**.

The build started on `qwen3-embedding` (the 8B tag) at 4096 dimensions. That was
never a decision — it was the default the model happened to emit, and 4096 is
four times the storage, four times the index memory, and four times the distance
computation per comparison. At a million tickets it is the difference between
16 GB of vectors and 4 GB.

Two ways to reach 1024 from Qwen3:

| Approach | Trade |
|---|---|
| **The 0.6B tag** (chosen) | Native 1024. Smaller and faster on the write path. Lower benchmark scores than 8B |
| Truncate the 8B's 4096 (Matryoshka) | Keeps the stronger model, but pays full 8B inference and discards three quarters of the output |

The 0.6B tag won on a design argument as much as a performance one: the
dimension is **probed from the model** at startup and treated as the authority,
never configured (`adapters/ollama_embeddings.py`). A native 1024 model
preserves that exactly — the change was one config line. Truncation would have
meant adding a slice-and-renormalise step plus a dimension setting, reintroducing
the hardcoded value the probe exists to eliminate.

**The switch invalidated every stored vector automatically.** `source_hash`
covers the model name, so changing the model makes every existing hash stale and
forces a re-embed rather than leaving a corpus half in each convention. That was
designed in long before it was needed, and it is the reason a model swap is a
config change rather than a migration.

Whether 1024 costs any measurable recall is answerable for free: re-seed the
100-ticket corpus and run `scripts/eval_retrieval.py`, which makes zero LLM
calls. That has not been done since the switch — the corpus is currently the
ten-ticket inspection set — so the honest statement is that the saving is
certain and the cost is unmeasured.

---

### The embedding console — who presses the button

Embedding is normally automatic: ticket written → outbox row → worker → vector.
`AI_AUTO_EMBED_ON_WRITE` gates that enqueue, and **it is currently off**, so
nothing is embedded until a human asks for it from `/embeddings`.

That is a development posture, not a recommendation. Automatic embedding on
write is the correct production default — a ticket raised at 3am is searchable
at 3am, not whenever an operator next remembers. Turning it off makes the
pipeline *observable* while it is being learned: a ticket sits visibly
un-embedded until the button is pressed.

The console is worth having either way, because three jobs stay human even when
automation is on:

* **Backfill** — a corpus that predates the AI layer.
* **Repair** — drift after a failure, which the outbox deliberately swallows
  (`emit()` logs and continues so a misconfigured AI layer can never block
  ticket creation).
* **Migration** — re-embedding everything after a model change.

**Three states, and the middle one is the point:**

| State | Meaning |
|---|---|
| `not_embedded` | No vector exists |
| `embedded` | A vector exists and matches the current text |
| `stale` | A vector exists, but the text changed after it was written |

`stale` is why the page is a join rather than a count. A stale ticket *looks*
indexed, so a simple "how many have vectors" query reports it as healthy — while
retrieval returns it confidently for text that no longer exists. That is worse
than having no vector at all.

**It runs synchronously**, which inverts how the automatic path works. The
reason is the audience: a person is watching. A queued job means the page says
"submitted" and the operator has to go and find out whether it worked. The trade
is bounded rather than ignored — `MAX_SYNC_BATCH` caps one request at 100
tickets and refuses beyond it rather than silently truncating, because an
operator who asked for 500 and got 100 will believe the other 400 are done.
Bulk corpus migration belongs on the queue, and `scripts/ai_backfill.py` is that
path.

**It cannot edit a ticket.** The console owns the *index*, not the content: it
rebuilds and deletes vectors, and the text it embeds always comes from the
tickets table. No operator action can make the index disagree with the record it
describes.

Access is `embedding:manage`, held by team managers and tenant admins. A CS
agent can read every ticket in the tenant and still cannot rebuild the corpus —
reading content and operating the index are different resources.

---

## Worked scenario

**A tenant is onboarded and backfilled.**

```
1. Tenant created in Postgres (existing flow, unchanged).

2. First AI request for that tenant:
     get_or_create_collection("tickets_<hex>")
   → created lazily, with cosine space and the detected dimension.

3. Backfill enqueues one embed job per existing ticket:
     100 rows into ai_jobs.

4. Worker drains them, batching 32 at a time through Ollama
   (batching matters — 100 single calls is ~10x slower than 4 batched ones).

5. ticket_vector_state now has 100 rows. Reconciliation reports zero drift.
```

**A tenant is queried, and isolation holds.**

```
Tenant A opens OS-193, similar-issue search runs:
  collection = tickets_<A_hex>
  → 12 candidates, all tenant A's

Tenant B does the same:
  collection = tickets_<B_hex>
  → 0 candidates (B has no tickets yet)

There is no query B could construct that reaches A's collection, because the
collection name is derived from B's authenticated tenant id.
```

**A ticket is edited.**

```
PATCH /api/v1/tickets/OS-193  (description changed)
  → enqueues embed job unconditionally
  → worker computes source_hash → differs from stored
  → re-embeds, upserts the same Pinecone id
  → ticket_vector_state.source_hash updated

If only the title's whitespace changed, the normalised hash matches and the job
is a no-op.
```

---

## Interview questions

**Q: Why a vector database rather than storing vectors in Postgres?**

Honestly, for this system pgvector would be the better engineering choice —
vectors inside the database that already enforces row-level security, one store
to operate, and the embedding write in the same transaction as the ticket. We
chose a managed vector database deliberately for the learning goal, and I can articulate exactly
what that costs: a dual write between two stores, which needs a transactional
outbox and a reconciliation job to stay consistent. That is the trade-off, and I
would revisit it if drift became an operational problem.

**Q: How do you isolate tenants in a vector store?**

Collection per tenant, with the name derived from the authenticated tenant id.
The alternative — a shared collection with a metadata filter — makes isolation
depend on every query remembering the filter, and one omission is a
cross-tenant leak with nothing underneath to catch it. With separate
collections, opening the wrong one returns nothing. I care about that asymmetry:
a bug that returns nothing gets noticed and fixed; a bug that returns another
tenant's data might never be noticed.

**Q: HNSW versus IVFFlat versus exact search — how do you choose?**

Exact search is fine until the corpus grows; it is linear in the number of
vectors, so at a few thousand it is genuinely competitive and perfectly
accurate. IVFFlat partitions the space into lists, which needs a training step
on a representative sample — awkward when the corpus is still growing. HNSW
builds a navigable graph, needs no training, handles continuous inserts, and
gives better recall at low latency. Since tickets arrive one at a time forever,
HNSW fits the workload. Its cost is a slower build and more memory.

**Q: What happens if Pinecone is down when a ticket is created?**

Nothing visible. Ticket creation only writes Postgres — the ticket row and an
outbox job row, in one transaction. The worker discovers Pinecone is unreachable,
the job fails, and it retries with backoff. The ticket exists and is fully
usable; it just has no vector yet, so it will not appear in similarity results
until the worker catches up. That is the point of keeping AI off the critical
path: an AI dependency being down degrades an AI feature, not the product.

**Q: How do you know Postgres and Pinecone have not drifted?**

A bookkeeping table, `ticket_vector_state`, in Postgres, recording which tickets
have been embedded with which model and content hash. Reconciliation compares
three sets: tickets with no state row (missing vectors), state rows whose ticket
is gone (stale vectors), and hash mismatches (out-of-date vectors). Each
discrepancy enqueues the appropriate job. Without that table, answering "which
tickets are missing vectors?" means reading every id out of Pinecone and diffing,
which is neither transactional nor scalable.

**Q: Cosine or Euclidean distance, and does it matter?**

Cosine, and it matters. Cosine compares direction and ignores magnitude;
Euclidean is sensitive to both. For text embeddings, magnitude largely tracks
length, so with L2 a long ticket and a short ticket describing the same bug can
score as dissimilar purely because one has more text. The metric is fixed at index creation, so
this has to be set explicitly at collection creation — and it cannot be changed
afterwards without rebuilding the collection.

---

## Gotchas

- **The metric is fixed at index creation.** Choose `cosine`; it is not
  changeable later.
- **The dimension is fixed at creation.** Changing embedding models means a new
  collection, which is why `(ticket_id, model)` keying matters.
- **Embedded mode plus two processes corrupts SQLite.** Use server mode.
- **`upsert`, not `add`** — otherwise a re-embed either raises or silently
  duplicates.
- **Pinecone returns similarity, Chroma returned distance.** Whichever store is in use, convert once at the adapter boundary — carrying the wrong direction across a port inverts every ranking silently.
- **Batch the embed calls** during backfill. Individual Ollama round-trips
  is roughly ten times slower than batches of 32.
- **Deleting a ticket must delete its vector.** Soft delete in Postgres means
  the vector should be removed from Pinecone too, or a deleted ticket keeps
  surfacing in similarity results.
