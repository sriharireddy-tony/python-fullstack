# 04 — RAG & Retrieval

## What it is

RAG — Retrieval-Augmented Generation — means: don't ask the model what it
remembers, find the relevant documents first and have it reason over *those*.

For us the documents are past tickets. The pipeline is **hybrid retrieval → rank
fusion → grading → optional rewrite loop → rerank**, and the reranking LLM only
ever sees about twenty candidates, never the whole corpus.

---

## Why we chose this shape

### Why hybrid, and not vector search alone

This is the decision that most affects quality.

Bug reports are full of **literal tokens that embeddings blur**:

```
"PF challan"        "OS-1042"        "500 on /payroll/export"
"Form 16"           "80C"            "TypeError: NoneType"
```

Embeddings map those into a smooth semantic space, which is exactly wrong when
the user means *that exact string*. Lexical search matches them precisely.

Meanwhile:

```
"payslip is blank"  ←→  "salary slip shows no data"
```

share almost no words and are the same bug. Lexical search finds nothing.
Embeddings find it immediately.

Neither alone is sufficient, and the failures are complementary rather than
overlapping — which is the condition under which combining actually helps.

We already have the lexical half: `tickets.search_vector` is a generated
`tsvector` column with a GIN index, built in Phase 3 of the main application. It
costs nothing extra to use.

### Why rank fusion rather than weighted scores — and why we still dropped RRF

This section was rewritten after measuring, and the original reasoning is kept
because the correction is the interesting part.

**The starting argument, which is still right.** The two retrievers return
**incomparable numbers**. Cosine similarity is roughly 0–1 with useful results
bunched between 0.6 and 0.9. `ts_rank_cd` is unbounded and depends on document
length and term frequency. Blending them (`0.7 * cosine + 0.3 * rank`) needs
tuning data that does not exist before the feature ships, and the weights would
be wrong per query anyway. So use **rank, not score**:

```python
K = 60
scores = defaultdict(float)
for ranked_list in (semantic_hits, lexical_hits):
    for rank, ticket_id in enumerate(ranked_list, start=1):
        scores[ticket_id] += 1.0 / (K + rank)
```

Ranks are comparable by construction, a ticket in both lists is promoted
automatically, and there is no weight to pick.

**What the measurement said.** Reciprocal Rank Fusion, implemented exactly as
above, was **worse than either retriever alone**:

| configuration (all unaided)   | duplicates R@5 | duplicates R@20 | identifiers R@20 |
|-------------------------------|----------------|-----------------|------------------|
| semantic                      | **1.000**      | **1.000**       | 0.583            |
| lexical                       | 0.931          | 0.986           | **1.000**        |
| RRF, equal weight             | 0.986          | 1.000           | 0.792            |

Measured on 100 tickets with no verbatim repeats, 36 paraphrase queries and 24
identifier queries. RRF is below the better retriever on both families: by one
query at R@5 on duplicates, and by 0.208 at R@20 on identifiers.

RRF's whole value is its **agreement bonus** — a document both retrievers ranked
reasonably beats one a single retriever loved. That bonus is also its failure
mode when the retrievers are of unequal quality for the query at hand, and on
this corpus they always are, in whichever direction the query points. A ticket
one retriever ranked 35th and the other ranked 2nd outscores the ticket the
stronger retriever ranked **1st**:

```
1/(60+35) + 1/(60+2)  =  0.0266   >   1/(60+1)  =  0.0164
```

So the weaker retriever's confident junk displaces the stronger one's best
answer.

**Weighting the secondary retriever down does not fix it, and that is provable.**
Require that an agreement bonus can never lift a document above the leader's own
top hit:

```
1/(K + 2) + w/(K + 1)  ≤  1/(K + 1)
                    w  ≤  1/(K + 2)  =  0.016   at K = 60
```

At any weight small enough to be safe, the secondary retriever contributes
nothing but the ordering of documents *below* the leader's list. "Keep RRF's
agreement bonus" and "never rank worse than the better retriever" are not
simultaneously satisfiable. The choice has to be made, not tuned around.

**What ships instead: a cascade.** The leading retriever's order, with the
other's finds appended below it. Guarantees, at any cutoff up to the leader's
depth:

* the first *n* results are exactly the leader's first *n*, so it is **never
  worse than the leader alone**;
* documents only the secondary found are still present further down, so recall
  is at least the leader's and usually better;
* `agreed` is still populated, so the reranker and the UI keep the
  cross-retriever agreement signal even though ordering no longer uses it.

The reason to prefer recall over head precision here is concrete: the next stage
is an LLM reranker over this candidate pool. A reranker can reorder what it is
given but cannot recover a document retrieval never returned. The pool's job is
recall; precision at the head is the reranker's.

**And exact tokens bypass ranking entirely.** The first attempt at handling
identifiers was to pick the leader per query — "does this query contain an error
code?" It was measurably worse, because the similar-issues query *is* a ticket's
own text, and tickets routinely contain a code unique to themselves. The
heuristic fired on prose queries and handed ranking to the weaker retriever:
duplicate recall fell to keyword search's own score, because that is what the
pipeline had quietly become on every query.

The mistake was spending a fact as a hint. `ERR-40001` does not suggest which
retriever is better — it **names a ticket**. So it is looked up with AND
semantics and pinned above the ranking, exactly like an explicit `OS-142`
reference. That took the identifier family from 0.583 (semantic) and 1.000 at
depth 20 but only 0.625 at depth 5 (lexical) to **1.000 recall with MRR 1.000**
— the whole family answered at rank 1 — and left the duplicate family untouched
at 1.000.

**One dependency worth naming.** The cascade is the right fusion *given* the
probe, not on its own merits. Unaided on identifier queries the cascade scores
0.583 and RRF scores 0.792: RRF's agreement bonus lifts keyword search's finds
into the top 20, while the cascade appends them below the leader's twenty where
the depth cutoff drops them. RRF is genuinely the better fusion for that family.
It loses anyway, because the probe answers identifier queries deterministically
before any ranking happens, and RRF's head-precision cost would then be paid on
every paraphrase query to buy a rescue that never fires. If the probe is ever
removed, this decision has to be re-made — which is why `rrf, no probe` is a
permanent row in the ablation rather than a one-off check.

Two bounds keep the probe honest: at most three identifiers per query, and a
token is pinned only when it matches **exactly one** ticket. The second bound
was found by measurement too — allowing two- or three-way matches cost one
duplicate query its rank-1 result, because a shared `/api/v3/...` path pinned an
unrelated ticket.

**The lesson, stated once.** Separate facts from estimates. Look facts up; rank
estimates. Most of the difficulty in this pipeline came from trying to make one
mechanism do both.

**When to revisit:** once there is enough labelled data, a learned fusion or a
trained cross-encoder could beat a cascade. The ablation script is the gate that
would show it.

### Why a corrective loop (CRAG)

Naive RAG retrieves once and generates. If retrieval was poor, the model
confidently reasons over irrelevant documents — which is worse than saying
nothing, because the output looks authoritative.

Corrective RAG adds a grading step: are these candidates actually good? If not,
rewrite the query and retrieve again.

```
retrieve → grade ──good──→ rerank
              │
              └──weak──→ rewrite query ──→ retrieve (max 2 retries)
                                              │
                              still weak? → "no similar issues found"
```

**"No similar issues found" is a first-class answer.** The threshold is set high
enough to return it, because a feature that always shows something is
untrustworthy after the first bad suggestion.

### Why rerank at all

Vector top-k is recall-oriented and deliberately noisy. The rerank does the work
that makes the feature usable:

- decides whether a candidate is genuinely the *same bug*
- classifies it **duplicate / related / recurring**
- **explains why**, in one line

That explanation is the product. Nobody acts on a similarity score of 0.83. They
act on *"same payroll export failure, Northwind reported it in March, fixed in
OS-812"*.

### Why we do not chunk

Standard RAG chunks long documents. We do not, and it is deliberate: a ticket
title plus description is a few hundred tokens — already one coherent unit.
Chunking it would split a single bug report into fragments that then compete
with each other in the results, and we would have to merge them back. The
natural document boundary is the ticket.

---

## How it works technically

### Stage 1 — two retrievals, in parallel

**Semantic**, in Chroma:

```python
result = collection.query(
    query_embeddings=[query_vector],
    n_results=50,
    include=["distances", "metadatas"],
)
```

**Lexical**, in Postgres:

```sql
SELECT t.id, ts_rank_cd(t.search_vector, q) AS score
FROM tickets t, websearch_to_tsquery('english', :text) q
WHERE t.search_vector @@ q
  AND t.deleted_at IS NULL
  AND t.id <> :self_id
ORDER BY score DESC
LIMIT 50;
```

`websearch_to_tsquery` rather than `to_tsquery` — it accepts what people
actually type (quoted phrases, `OR`, a leading minus) instead of raising on
malformed input.

Neither query mentions `tenant_id`: Postgres has RLS, Chroma has the per-tenant
collection.

### Stage 1b — the exact lookups, before anything expensive

Two things in a query are *facts*, not similarity signals, and both are resolved
before a vector is ever computed:

* **A ticket reference.** `OS-1042` or a bare `1042` resolves straight to that
  ticket. People quote numbers far more than they search prose.
* **A literal identifier.** `ERR-40001`, `DEV_SYNC_408`, `EMP-1234`, a trace id,
  an `/api/v3/...` path. Looked up with AND semantics via `plainto_tsquery`, and
  pinned **only if it matches exactly one ticket** — otherwise it is a shared
  token, not an identifier, and its hits belong to the ranker.

Both go into `pinned`, which fusion places above the ranked results and the
score floor does not filter. Cost: two indexed queries.

### Stage 2 — fusion

A cascade, not RRF: the leading retriever's order with the other's finds
appended below, take the top 20. Semantic leads whenever it ran; keyword search
leads when it is the only retriever. See "Why rank fusion rather than weighted
scores" above for the measurement that produced this and rejected RRF.

### Stage 3 — grading

A cheap check, no LLM:

```python
def needs_rewrite(state) -> bool:
    top = state["candidates"][:5]
    return len(top) < 3 or max(c.rrf_score for c in top) < FLOOR
```

Only if this fails do we spend an LLM call rewriting the query. Grading first,
rewriting second, keeps the common case free.

### Stage 4 — query rewrite

One small LLM call, on the cheap model:

> Original: *"payslip issue"*
> Rewritten: *"payslip PDF empty blank no salary rows contractor download"*

Bug reports are often terse or use internal shorthand; expanding to likely
technical vocabulary meaningfully improves recall. Capped at two rewrites, then
we accept the answer is "nothing found".

### Stage 5 — rerank

One call with the twenty candidates, structured output:

```json
{
  "results": [
    {
      "ticket_id": "…",
      "relation": "duplicate",
      "confidence": 0.91,
      "reason": "Same blank-payslip failure for contractors; OS-201 was fixed in June."
    }
  ]
}
```

Structured output is a **security control**, not just convenience: the model
cannot return an action or an instruction, only entries in a fixed schema. And
every returned `ticket_id` is validated against the candidate set we sent — an
id we did not supply is dropped, so a hallucinated or injected reference cannot
reach the user.

### Stage 6 — threshold and cache

Below the confidence floor, drop it. Everything dropped → "no similar issues
found".

Cached under `tenant:{id}:similar:{ticket_id}:{ticket.version}`. Including
`version` means the cache **invalidates itself** when the ticket is edited: a new
version is a new key. Nothing to remember to purge.

### The rerank is optional, by design

```
enable_rerank = False  (or quota exhausted)
  → return RRF-ordered results, unclassified, no explanations
```

This is both the correct degradation in production **and** how development
happens without burning quota — retrieval can be built and measured entirely
offline.

---

## Worked scenario

Someone opens **OS-221**: *"Payslip download empty for contract staff"*.

```
STEP 1  Load OS-221's stored vector. No embedding call — it is already indexed.

STEP 2  Semantic (Chroma, tenant collection, n=50):
          OS-193  0.94   Payslip PDF generates blank for contractors
          OS-214  0.91   No earnings rows on payslip for non-permanent staff
          OS-201  0.89   Payslip PDF generates blank for contractors
          OS-108  0.71   Reimbursement claim stuck in pending
          OS-77   0.64   Long client names break the table layout
          …

        Lexical (Postgres FTS, limit 50):
          OS-193  0.42   (matches payslip, contractor)
          OS-201  0.39
          OS-166  0.21   (matches download)
          …
        Note OS-214 is ABSENT from lexical — it says "earnings rows" and
        "non-permanent", sharing almost no words. Only semantic found it.

STEP 3  Cascade (semantic leads, lexical appended below):
          OS-193   semantic #1, lexical #1   ← agreed
          OS-214   semantic #2               ← semantic only, and CORRECT
          OS-201   semantic #3, lexical #2   ← agreed
          OS-108   semantic #4
          …
          then anything lexical found that semantic did not, in lexical order.

        Note what would have happened under RRF here. OS-201 scores
        1/63 + 1/62 = 0.0320 and OS-214 scores 1/62 = 0.0161, so the
        agreed-but-weaker ticket outranks the semantically-closest one.
        On the golden set that pattern cost 0.053 recall@20 and 0.05 MRR,
        which is why the cascade ships. The `agreed` flag is still recorded
        and still shown to the reader -- it is good evidence, it is just not
        allowed to reorder the leader's list.

STEP 4  Grade: 4+ candidates above the floor → good. No rewrite needed.

STEP 5  Rerank (one LLM call, 20 candidates):
          OS-193  duplicate  0.94  "Identical failure; currently in progress."
          OS-201  recurring  0.88  "Same bug, closed in June — likely a regression."
          OS-214  duplicate  0.85  "Same blank-payslip failure, different wording."
          OS-108  unrelated  0.12  → dropped

STEP 6  Present:
          Duplicate   OS-193, OS-214
          Recurring   OS-201   ← the useful one: this was fixed and came back
```

**The value is in step 6.** OS-201 being flagged *recurring* rather than
*duplicate* tells the developer this is a regression and there is an earlier fix
to examine — which no similarity score alone could convey.

**A failing case, for contrast.** Someone opens a ticket titled *"issue"* with
description *"not working"*. Retrieval returns weak, scattered candidates.
Grading fails. Rewrite produces nothing better because there is nothing to
expand. After two attempts the answer is *"no similar issues found — try adding
detail to the description"*, which is the honest and correct output.

---

## Interview questions

**Q: Explain RAG to someone who has not heard of it.**

A language model only knows what was in its training data, and it will
confidently invent things it does not know. RAG fixes that by changing the
order: first search your own data for relevant documents, then hand those to the
model and ask it to answer using only them. The model becomes a reasoner over
retrieved evidence rather than a source of facts. For us it means the model never
guesses whether a bug was reported before — it is shown the candidate tickets
and asked to judge which are genuinely the same.

**Q: Your retrieval returns poor results. How do you debug it?**

I separate the stages, because they fail for different reasons and only one is
fixable by prompting. First, is it a *retrieval* problem or a *ranking* problem?
`recall@20` answers that: if the known duplicate is not in the top twenty, no
amount of prompt work will help, and the problem is in embedding, chunking, or
query construction. If recall is fine but `precision@5` is poor, the reranker is
the problem and the prompt is worth tuning. Keeping those two metrics separate is
what makes the failure diagnosable — a single end-to-end score tells you it is
bad but not where.

**Q: Why Reciprocal Rank Fusion instead of weighting the two scores?**

Because the scores are not on comparable scales. Cosine similarity clusters
between about 0.6 and 0.9 for useful results; `ts_rank_cd` is unbounded and
varies with document length. Combining them with weights requires labelled data
to tune those weights against, and that data does not exist before shipping. RRF
uses rank position instead, which is comparable by construction, and has no
parameters beyond a conventional constant. If a document ranks well in both
lists it rises automatically. Once we have enough acceptance data, a learned
fusion could beat it — but starting with a method that has no knobs means
nothing to mis-tune.

**Q: What is corrective RAG and why do you need it?**

Naive RAG retrieves once and generates regardless of what came back. If
retrieval failed, the model reasons over irrelevant documents and produces a
confident, wrong answer — which is worse than nothing because it looks
authoritative. Corrective RAG adds a grading step between retrieval and
generation: judge whether the candidates are actually relevant, and if not,
rewrite the query and retrieve again. In LangGraph that is a conditional edge
creating a cycle, capped at two retries. The important part is that the loop can
terminate in "I found nothing", which is a legitimate and frequently correct
answer.

**Q: Why do you rerank? Isn't the vector search already ranked?**

It is ranked by geometric closeness, which is not the same as being the same
bug. Two tickets can be textually close and describe different problems, and the
vector score cannot tell you which. The reranker makes the judgement call, and
more importantly it classifies the relationship — duplicate, related, or
recurring — and explains it in a sentence. Nobody acts on "similarity 0.83".
They act on "same payroll export failure, Northwind reported it in March, fixed
in OS-812". Reranking only the top twenty is also what makes it affordable: the
expensive model sees twenty candidates, not five thousand tickets.

**Q: How do you decide chunk size?**

For us: we do not chunk, and that is the considered answer rather than an
omission. A ticket title plus description is a few hundred tokens and already
one coherent unit — the natural document boundary is the ticket. Chunking would
split one bug report into fragments that compete with each other in the results,
and we would then have to deduplicate and merge them back. Chunking earns its
place when documents are long and heterogeneous, like a manual or a contract,
where a single embedding cannot represent the whole thing. Applying it reflexively
to short documents adds complexity and costs quality.

**Q: How would you add filtering — say, only search Payroll tickets?**

Two places, and which one you pick matters for recall. Chroma supports a `where`
clause that filters before the ANN search, so the nearest-neighbour computation
happens within the filtered set — that is correct but can be slow if the filter
is very selective. Filtering *after* retrieval is faster but risks the top-k all
being excluded, leaving nothing. For a broad filter like team, pre-filtering is
right. For a narrow one, I would over-fetch and post-filter. The general rule:
the more selective the filter, the more you must over-fetch.

---

## Gotchas

- **`to_tsquery` raises on malformed input.** Use `websearch_to_tsquery`.
- **Exclude the ticket itself** from its own similar-issues results — obvious,
  and easy to forget until it appears at the top with similarity 1.0.
- **Exclude soft-deleted tickets** in the lexical query; Chroma needs them
  removed on delete.
- **RRF needs ranks, not scores.** Passing scores in silently reintroduces the
  normalisation problem it exists to avoid.
- **Validate returned ids against the candidate set.** Otherwise a hallucinated
  reference reaches the user.
- **Cache by ticket version**, not ticket id, or an edited ticket keeps serving
  stale suggestions.
- **A high floor is a feature.** Tuning the threshold down to "always return
  something" is how this feature loses trust.
