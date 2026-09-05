# 02 — Embeddings

## What it is

An embedding turns text into a fixed-length list of numbers, positioned so that
texts with similar meaning end up close together in that space. "Payslip is
blank" and "salary slip shows no data" share almost no words, but their vectors
sit near each other.

We generate one embedding per ticket and store it, so that finding similar
tickets becomes a geometry problem instead of a keyword problem.

**Model:** `qwen3-embedding` served locally by Ollama.

---

## Why we chose this

### Why local rather than a hosted embeddings API

Embedding is the one stage that touches **every ticket ever written**, plus every
re-embed after a model change. Ticket descriptions contain payroll figures,
employee names, and government identifiers — and that data belongs to our
customers' employees, not to us.

| | Text leaving the network |
|---|---|
| **Embeddings** | Every ticket, every time the model changes |
| Rerank | ~20 candidates per query, fields we choose |

So the stage with the largest egress is also the stage where model quality
matters *least* — near-duplicate detection is one of the easier embedding tasks.
Running it locally costs nothing per call, which means re-embedding the whole
corpus is a CPU decision rather than a budget one.

That freedom matters more than it sounds. You *will* change the embedding model
at least once, and with a hosted API that is a line item you have to justify.

### Why Qwen3-Embedding specifically

It is already pulled locally, and it is a strong retrieval model — better than
Qwen2 for this task. It is also **MRL-trained** (Matryoshka Representation
Learning), which means its vectors can be truncated to fewer dimensions with
graceful degradation rather than a cliff. That gives us a storage lever later
without re-embedding.

### Alternatives considered

| Option | Why not |
|---|---|
| Hosted embeddings (Voyage, OpenAI) | Every ticket leaves the network. Marginal quality gain on an easy task |
| `nomic-embed-text` / `mxbai-embed-large` | Fine models, but Qwen3 is already local and scores better on retrieval |
| Anthropic embeddings | **Do not exist** — Anthropic has no embeddings endpoint |
| TF-IDF / BM25 only | That is our lexical half already. It cannot match paraphrase |

**When to revisit:** if `recall@20` measured against the golden set is
unacceptable. Measure before switching — the bottleneck is far more likely to be
the rerank prompt.

---

## How it works technically

### Dimension detection, not a hardcoded number

The dimension is baked into the Chroma collection at creation. Getting it wrong
means rebuilding the collection. So it is **probed once at startup** rather than
configured:

```python
async def detect_dimension(model: str) -> int:
    """Ask the model for one vector and measure it.

    Cheaper and more reliable than maintaining a table of model dimensions,
    which goes stale the moment someone pulls a different quantisation.
    """
    vector = await ollama_embed(model, "dimension probe")
    return len(vector)
```

`qwen3-embedding` at 4.7 GB is most likely the 8B variant → **4096 dimensions**,
but the probe is authoritative.

### The instruction asymmetry — the one real gotcha

Qwen3-Embedding is **instruction-aware and asymmetric**. Queries are supposed to
carry a task-instruction prefix; documents are not. Embed both the same way and
you quietly lose retrieval quality — nothing errors, the results are just worse.

The interface makes it impossible to get wrong:

```python
class EmbeddingProvider(Protocol):
    dimension: int
    async def embed_query(self, text: str) -> list[float]: ...
    async def embed_documents(self, texts: list[str]) -> list[list[float]]: ...
```

**Two methods, not one.** There is no single `embed()` that could be called with
the wrong intent. The prefixes live in config:

```python
QUERY_INSTRUCTION = (
    "Instruct: Given a software bug report, retrieve other bug reports "
    "describing the same underlying problem\nQuery: "
)
DOCUMENT_INSTRUCTION = ""   # documents are embedded bare
```

**Both strings are part of the embedding identity.** Change either and every
stored vector is stale, so the instruction version goes into `source_hash`
(below) — which forces a re-embed automatically rather than leaving a corpus
half in one convention and half in the other.

### What text gets embedded

Not the whole ticket:

```python
text = "\n\n".join(filter(None, [
    ticket.title,                              # highest signal per token
    ticket.description[:2000],
    (ticket.steps_to_reproduce or "")[:1000],
]))
```

**Bounded**, because the model truncates at its token limit anyway — and an
unbounded field means the first N tokens silently decide the vector, with no
indication of what got cut.

**Title first**, because if truncation happens the title is what you least want
to lose.

Deliberately excluded, each for a reason:

| Excluded | Why |
|---|---|
| Comments | They change constantly — including them means re-embedding forever |
| Resolution notes | Written *after* the fact. Including them leaks the outcome into a signal used at triage time |
| Reporter name/email | Noise, and unnecessary PII in the vector pipeline |
| Client name | Would cluster tickets by client rather than by problem |

That last one is subtle and worth keeping: if the client name is in the embedded
text, "Acme payroll bug" and "Acme leave bug" look similar because they share
"Acme". We want similarity by *problem*, and the client is a metadata filter.

### `source_hash` — what makes it idempotent

```python
source_hash = sha256(f"{model}|{instruction_version}|{normalise(text)}").hexdigest()
```

Before embedding, the worker checks whether a row already exists for
`(ticket_id, model, source_hash)`. A hit means nothing has changed — skip.

This makes the pipeline idempotent, and that has three concrete payoffs:

1. A retried job costs one cheap query, not one embed call.
2. A full backfill over an already-embedded corpus is nearly free.
3. Ticket edits are correct without any change detection — `PATCH /tickets`
   always enqueues an embed job, and the hash decides whether work is needed.

Including the **model** and **instruction version** in the hash is what makes a
config change force a re-embed instead of silently mixing conventions.

---

## Worked scenario

CS raises OS-221: *"Payslip download empty for contract staff"*.

```
1. POST /api/v1/tickets commits, inside one transaction:
     tickets row
     ai_jobs row  (kind='embed_ticket', payload={ticket_id})
   201 returns immediately — no embedding yet.

2. Worker claims the job (FOR UPDATE SKIP LOCKED).

3. Builds the text:
     "Payslip download empty for contract staff\n\n
      Downloading a payslip for a contractor produces a PDF with
      headers but no salary rows.\n\n
      1. Sign in as HR admin  2. Open Payroll ..."

4. source_hash = sha256("qwen3-embedding|v1|payslip download empty...")
   → no existing row → proceed.

5. Ollama:
     POST http://localhost:11434/api/embed
     { "model": "qwen3-embedding", "input": "<the text, no prefix>" }
   → 4096 floats

6. Upsert into Chroma collection tickets_<tenant_id>:
     id       = str(ticket.id)
     embedding = [...4096 floats...]
     metadata = { ticket_number, team_id, client_id, status, created_at,
                  source_hash }
     document = None            ← text stays in Postgres only

7. Record ticket_vector_state(ticket_id, model, source_hash, synced_at).
```

Later, when someone opens OS-221 and we search for similar tickets, we **do not
call the embedding model at all** — the vector is already stored. The model is
only invoked on the write path.

---

## Interview questions

**Q: How do you choose an embedding model?**

By what the task needs and what the data constraints allow. Ours is
near-duplicate detection over short technical text — an easy retrieval task
where a good open model is sufficient. The binding constraint was data
sensitivity: embedding touches every ticket, and the text contains third-party
PII, so a hosted API was ruled out on privacy rather than quality grounds. Then
between local options, Qwen3-Embedding scored best on retrieval and was
MRL-trained, which gives a dimension lever later. Crucially we built the
evaluation harness *before* committing, so "is this model good enough" is a
measurement rather than an opinion.

**Q: What is the difference between symmetric and asymmetric embedding, and why
does it matter?**

Symmetric means query and document are the same kind of text and get embedded
identically — sentence similarity, for example. Asymmetric means a short query
retrieves longer documents, and the model expects them treated differently.
Qwen3-Embedding is asymmetric and instruction-aware: queries take a task prefix,
documents do not. Getting it wrong causes no error, just quietly worse recall.
We encoded it into the interface as two separate methods, `embed_query` and
`embed_documents`, so there is no single function that could be called with the
wrong intent.

**Q: Your embedding model produces 4096-dimension vectors. Is that a problem?**

It costs storage and search time: 4096 × 4 bytes is 16 KB per vector, so ten
thousand tickets is about 160 MB plus index overhead, and ANN search over wider
vectors is slower. Qwen3-Embedding is MRL-trained, so we can truncate
client-side to 1024 and re-normalise, cutting storage roughly four times. But
whether that hurts recall is an empirical question, so the plan is to build at
full width, measure `recall@20` against the golden set, and truncate only if the
numbers hold. Choosing the dimension before measuring would be guessing.

**Q: How do you handle re-embedding when you change models?**

Three things make it safe. The embeddings live in their own store keyed by
`(ticket_id, model)`, so two model versions coexist while a backfill runs — no
downtime, and the switch is a config change. The model name and instruction
version are part of `source_hash`, so changing either invalidates every row
automatically instead of leaving a corpus half in each convention. And the
backfill reuses the same idempotent job path as normal ingestion, so it is
resumable and re-runnable.

**Q: Why not embed the comments too? Surely more context is better.**

Two reasons. Comments change constantly, so including them means re-embedding a
ticket every time someone replies — the vector never settles. And resolution
notes are written *after* the bug is understood, so embedding them leaks the
outcome into a signal we use at triage time, when that information does not
exist yet. That inflates offline evaluation scores and produces a system that
performs worse in reality than in testing. The agent reads comments through a
tool when it needs them; they just do not belong in the similarity vector.

---

## Gotchas

- **The dimension is baked into the Chroma collection.** Detect it, do not
  configure it.
- **The instruction prefix is part of the embedding identity.** Put it in
  `source_hash` or you will end up with a mixed corpus and unexplainable recall.
- **Do not embed the client name.** It clusters tickets by customer rather than
  by problem.
- **Ollama's first call after idle is slow** — the model has to load. Warm it at
  worker startup with a throwaway embed, or the first real job looks broken.
- **Normalise before hashing** (whitespace, case for the hash only) or trivial
  edits trigger pointless re-embeds.
- **Truncating an MRL vector requires re-normalising** afterwards, or cosine
  distances are wrong.
