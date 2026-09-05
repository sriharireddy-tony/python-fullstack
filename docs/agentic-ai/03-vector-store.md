# 03 — Vector Store

## What it is

A database built to answer one question fast: *given this vector, which stored
vectors are closest to it?* Ordinary indexes cannot do that — B-trees order
values on a line, and "close in 4096 dimensions" is not a range scan.

We use **Chroma**, with **one collection per tenant**.

---

## Why we chose this

### Why Chroma

It is the requested stack. Beyond that: it runs locally with one command, its
Python API is small enough to learn in an afternoon, and it is the vector store
named most often in job descriptions — which matters given the goal here.

### Why one collection per tenant — the important decision

This is the part to get right, because it is where the existing safety guarantee
does *not* carry over.

In Postgres, tenant isolation is enforced by **row-level security** with
`FORCE ROW LEVEL SECURITY`, verified end-to-end by 10 tests and a CI gate. A
developer who forgets a `WHERE tenant_id = ?` is caught by the database.

**Chroma has no equivalent.** Two options:

| Approach | Isolation rests on |
|---|---|
| Shared collection + `where={"tenant_id": ...}` | Every single query remembering the filter |
| **One collection per tenant** | Which collection you opened |

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

### Why no ticket text in Chroma

Chroma *can* store the document alongside the vector. We store only the id and
minimal metadata, and hydrate the actual ticket from Postgres.

**Why:** sensitive text then lives in exactly one place. One store to back up,
one to secure, one to satisfy a deletion request against. A second copy of
payroll screenshots in a store with weaker access control is a liability with no
upside — we need the ids to look the tickets up anyway.

Cost: debugging is slightly harder, since you cannot read the matched text
directly in Chroma. Acceptable.

### Alternatives considered

| Option | Why not here |
|---|---|
| **pgvector** | Genuinely the better engineering choice — vectors inside the database that already enforces RLS, one store, transactional consistency. Not chosen because Chroma is the requested stack and the learning goal is explicit |
| Qdrant / Weaviate | Better at tens of millions of vectors; both add a service and a second access-control model for a corpus of thousands |
| Pinecone | Hosted, so every vector leaves the network — which defeats the point of local embeddings |
| FAISS | A library, not a store. No persistence, no metadata filtering, no server |

**When to revisit:** if drift between Postgres and Chroma becomes a recurring
operational problem, pgvector removes the entire class of issue.

---

## How it works technically

### Server mode, not embedded

```bash
pip install chromadb
chroma run --path ./var/chroma --port 8001
```

**Why server and not embedded-persistent:** FastAPI and the outbox worker are
two separate processes. Embedded Chroma is backed by SQLite, and two writers
against one SQLite file is a corruption risk. A server serialises access
properly.

Also: no Docker on this machine, so the server runs as a native process rather
than a container.

### Collection setup

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

**`hnsw:space` must be `cosine`.** Chroma defaults to L2 (Euclidean). For text
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
are there so a search can be narrowed *inside* Chroma before results come back.

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

Chroma returns **distance**; smaller is closer. Converting to a similarity score
where bigger is better avoids a whole family of inverted-comparison bugs
downstream.

### The sync problem — and the honest cost of two stores

The ticket lives in Postgres. The vector lives in Chroma. Writing both is a
**dual write**, and dual writes fail in ways single writes do not:

| Failure | Result if handled naively |
|---|---|
| Write Chroma, then Postgres rolls back | A vector for a ticket that does not exist |
| Commit Postgres, then Chroma write fails | A ticket with no vector, and **nothing records that it was missed** |

The transactional outbox solves it:

```
┌──────────── one Postgres transaction ────────────┐
│  INSERT tickets                                  │
│  INSERT ai_jobs (kind='embed_ticket')            │
│  COMMIT                                          │
└──────────────────────────────────────────────────┘
                    │
        worker claims the job, writes Chroma,
        records ticket_vector_state, marks done
        (failure → retry with backoff)
```

The job's existence is atomic with the ticket's. Chroma becomes **eventually**
consistent rather than possibly-never consistent.

### Reconciliation — because eventually consistent still drifts

A periodic job compares the two sides:

```
tickets in Postgres with no ticket_vector_state row    → enqueue embed
ticket_vector_state rows whose ticket is deleted       → delete from Chroma
ids in Chroma with no ticket_vector_state row          → orphan, delete
source_hash mismatch between the two                   → enqueue re-embed
```

`ticket_vector_state` in Postgres is the bookkeeping table that makes this
possible — without it, "which tickets are missing vectors?" requires reading
every id out of Chroma and diffing, which does not scale and cannot be done
transactionally.

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
  → re-embeds, upserts the same Chroma id
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
chose Chroma deliberately for the learning goal, and I can articulate exactly
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

**Q: What happens if Chroma is down when a ticket is created?**

Nothing visible. Ticket creation only writes Postgres — the ticket row and an
outbox job row, in one transaction. The worker discovers Chroma is unreachable,
the job fails, and it retries with backoff. The ticket exists and is fully
usable; it just has no vector yet, so it will not appear in similarity results
until the worker catches up. That is the point of keeping AI off the critical
path: an AI dependency being down degrades an AI feature, not the product.

**Q: How do you know Postgres and Chroma have not drifted?**

A bookkeeping table, `ticket_vector_state`, in Postgres, recording which tickets
have been embedded with which model and content hash. Reconciliation compares
three sets: tickets with no state row (missing vectors), state rows whose ticket
is gone (stale vectors), and hash mismatches (out-of-date vectors). Each
discrepancy enqueues the appropriate job. Without that table, answering "which
tickets are missing vectors?" means reading every id out of Chroma and diffing,
which is neither transactional nor scalable.

**Q: Cosine or Euclidean distance, and does it matter?**

Cosine, and it matters. Cosine compares direction and ignores magnitude;
Euclidean is sensitive to both. For text embeddings, magnitude largely tracks
length, so with L2 a long ticket and a short ticket describing the same bug can
score as dissimilar purely because one has more text. Chroma defaults to L2, so
this has to be set explicitly at collection creation — and it cannot be changed
afterwards without rebuilding the collection.

---

## Gotchas

- **Chroma defaults to L2.** Set `hnsw:space: cosine` at creation; it is not
  changeable later.
- **The dimension is fixed at creation.** Changing embedding models means a new
  collection, which is why `(ticket_id, model)` keying matters.
- **Embedded mode plus two processes corrupts SQLite.** Use server mode.
- **`upsert`, not `add`** — otherwise a re-embed either raises or silently
  duplicates.
- **Chroma returns distance, not similarity.** Convert once, at the boundary.
- **Batch the embed calls** during backfill. Individual Ollama round-trips
  is roughly ten times slower than batches of 32.
- **Deleting a ticket must delete its vector.** Soft delete in Postgres means
  the vector should be removed from Chroma too, or a deleted ticket keeps
  surfacing in similarity results.
