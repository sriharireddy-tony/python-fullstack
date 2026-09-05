# 13 — Interview Prep

Consolidated scenario questions across every topic, plus how to present this
project.

Each document has its own question set covering its area. This one covers the
**cross-cutting** questions — the ones that span components, where a shallow
answer shows and a considered one stands out.

---

## How to present this project

### The 60-second version

> I built a multi-tenant support-to-engineering ticket tracker — FastAPI,
> PostgreSQL with row-level security, React — and then added an AI layer on top
> of it. The AI layer does three things: it surfaces previously-raised bugs
> similar to the one you are looking at, it runs an on-demand agent that
> investigates a ticket across the whole history and writes an analysis, and it
> exposes a stateful chatbot over the ticket data.
>
> The retrieval side is hybrid — Postgres full-text plus vector search in Chroma
> over local Qwen3 embeddings, fused by reciprocal rank, with a corrective loop
> that grades its own retrieval and retries with a rewritten query. The agent is
> a LangGraph ReAct loop over read-only tools. Everything is evaluated: retrieval
> metrics on every change, RAGAS weekly, and acceptance rate from real use.
>
> The part I would talk about longest is the guardrails, because ticket text is
> written by people outside the organisation, which makes it attacker-controlled
> input to a prompt.

### What makes it stand out

Most portfolio RAG projects are: load PDFs, embed, retrieve, answer. The
differentiators here, in rough order of how much they impress:

| Differentiator | Why it lands |
|---|---|
| **Multi-tenancy with a real isolation story** | Almost nobody handles this. You can explain RLS, why Chroma needed collection-per-tenant, and how you tested it |
| **Evaluation that gates changes** | Having numbers, and an ablation table proving hybrid beats either half, is rare |
| **Guardrails you can justify individually** | Rather than "we used a guardrails library" |
| **Tool-result injection** | Almost nobody has thought about mid-loop injection |
| **Knowing when *not* to use an agent** | Saying "similarity should not be an agent, and here is why" shows judgement |
| **A real constraint handled well** | 20 requests/day forced the model registry, caching, and local fallback |

### What to be honest about

Interviewers respect a candidate who states limits precisely:

- It runs on synthetic data, deliberately — real ticket data contains third-party
  PII and the free API tier permits training on submissions
- Retrieval quality is measured on 100 tickets, which is small
- There is a working evaluation harness but not yet a long enough baseline to
  show trend improvement
- pgvector would have been the better engineering choice; Chroma was chosen
  deliberately and the cost is a dual write needing an outbox and reconciliation

That last one is a strong answer, not a weak one. Naming the better alternative
and its trade-off is what a senior engineer does.

---

## Cross-cutting questions

### Q: Design a system that finds duplicate bug reports. Talk me through it.

Start with the data, not the model. Bug reports are short — a title, a
description, maybe repro steps — so the natural document unit is the ticket and
there is no chunking decision to make. They are also full of literal tokens that
embeddings blur: error strings, module names, ticket references. That
immediately tells me pure vector search is insufficient.

So: hybrid retrieval. Vector search for paraphrase — "payslip is blank" and
"salary slip shows no data" share no words and are the same bug. Keyword search
for the literals. Fuse the two ranked lists with reciprocal rank fusion, which
uses rank rather than score so there are no weights to tune against data I do
not have yet.

Then a reranker over the top twenty. Not to reorder for its own sake, but to
make a judgement the score cannot: is this *the same bug*, a related bug in the
same area, or a bug that was fixed and has come back? Those three need different
actions, and collapsing them into "similar" is the main reason this kind of
feature gets ignored.

Two things I would build early that people skip. An evaluation harness with a
labelled set, because retrieval recall is the ceiling and I need to know it
before tuning any prompt. And "nothing found" as a first-class answer — a
threshold tuned to always return something destroys trust after the first bad
suggestion.

### Q: Your RAG system hallucinates. Walk me through debugging it.

I would localise before theorising, because "hallucination" has at least four
distinct causes.

First, was the right context even retrieved? `recall@k` against the golden set
answers that. If the supporting document was not in the candidates, the model had
nothing to ground on and hallucination is the *expected* behaviour — the fix is
in retrieval, not the prompt.

Second, if retrieval was fine, is the answer unsupported by context that *was*
provided? That is faithfulness, and RAGAS measures it. A low faithfulness score
with good recall points at the generation prompt — usually it is not instructed
firmly enough to answer only from context, or the context is buried among noise.

Third, check the guardrails. We validate that every ticket reference in the
output appears in what the tools actually returned. If a fabricated reference is
reaching users, that check is missing or broken — and that is a much better fix
than prompt tuning, because it is deterministic.

Fourth, and people forget this: is it actually hallucination, or is the retrieved
data wrong? A confident answer citing a real ticket that says something different
is a data problem, not a model problem.

### Q: How would you reduce the cost of this system by 80%?

In order of how much they return relative to the effort:

Caching first, because it is free quality-wise. Similar-issue results are cached
by ticket id and version, so opening the same ticket repeatedly costs nothing,
and the cache self-invalidates on edit. Prompt caching on the stable prefix
matters most for the agent, where the full history is resent every turn — the
tool schemas and system prompt are byte-identical across runs, so caching them
roughly halves the cost per run.

Then routing. Reranking and query rewriting are classification tasks that do not
need a frontier model; they run on the cheap fast model at temperature zero. Only
the agent's multi-turn reasoning gets the stronger one. That is what the model
registry exists for.

Then eliminating calls. Retrieval works with no LLM at all, so the rerank is an
enhancement rather than a dependency — turning it off is a valid configuration
that keeps the feature useful. And the grading step means we only pay for a query
rewrite when retrieval was actually weak, not every time.

Then measurement. Every call records tokens and cost against a role and tenant,
so I can compute **cost per accepted suggestion** rather than cost per call.
That is the number that tells you where the waste actually is — and it has
occasionally shown that the expensive path was the one users valued.

### Q: What would you do differently if you rebuilt this?

Three things.

I would use pgvector rather than Chroma. The vectors would live in the database
that already enforces tenant isolation, the embedding write would be in the same
transaction as the ticket, and the dual-write problem — with its outbox and
reconciliation job — would not exist. Chroma was chosen deliberately for
learning, and I can articulate exactly what it cost.

I would build the evaluation harness before the retrieval pipeline, not
alongside it. I built it early, but not first, and there was a period where I was
tuning against intuition.

And I would ship the accept/reject control in the very first release. It is the
mechanism that grows the labelled set, so every week without it is a week of
lost training signal.

### Q: How do you decide between fine-tuning, RAG, and prompt engineering?

By what the model is missing. If it lacks *facts* — our ticket history, which
changes daily — that is RAG, and fine-tuning would be both expensive and stale
immediately. If it lacks a *format or style* you cannot get from instructions,
that is fine-tuning. If it has the knowledge and the capability but is applying
them badly, that is prompt engineering.

For us it is unambiguously RAG: the knowledge is a few thousand tickets that
change every day, and the model needs to reason over them rather than memorise
them. Fine-tuning would need retraining on every new ticket and would still
hallucinate ticket numbers. I would only reach for fine-tuning if we needed a
consistent output style that structured output could not express.

### Q: Walk me through what happens when a user opens a ticket.

The HTTP request hits FastAPI, authenticates from a JWT in an httpOnly cookie,
and the tenant id from that token is set as a Postgres session variable so
row-level security scopes every query. So far, no AI.

Then the similar-issues call. The ticket's embedding is already stored — the
write path embedded it asynchronously when the ticket was created, so no
embedding model is invoked here. Two retrievals run in parallel: a cosine search
in that tenant's Chroma collection, and a Postgres full-text search over the same
text. Both come back ranked, and they are fused by reciprocal rank.

A grading node checks whether the candidates are good enough. If they are weak,
a conditional edge routes to a query-rewrite node and we retrieve again, up to
twice. If they are fine, the top twenty go to a reranker, which returns each
classified as duplicate, related, or recurring with a one-line reason.

Then output guardrails: the response is schema-validated, and every ticket
reference is checked against the candidate set we actually sent — anything else
is dropped. Results below the confidence floor are discarded, and if that leaves
nothing, the answer is "no similar issues found". Finally it is cached by ticket
id and version.

One to three seconds cold, instant cached, and one LLM call — or zero if
reranking is disabled.

### Q: How would you scale this to a million tickets?

Three things change, in order of when they would bite.

The vector index. At a million vectors, HNSW parameters start to matter —
`ef_construction` and `M` become real tuning decisions rather than defaults. And
with many tenants, per-tenant collections mean many small indexes rather than one
large one, which is actually better for both search time and isolation.

Retrieval breadth. With a corpus that large, twenty candidates from fusion is a
smaller fraction of the relevant set, so I would over-fetch more and probably add
a cheap pre-filter — search within the same product module first, fall back to
global. Metadata filtering before the ANN search matters much more at scale than
it does at thousands.

Evaluation, and this is the one people miss. A golden set of twenty pairs is
adequate at 100 tickets and meaningless at a million — the labelled set has to
grow with the corpus, which is another argument for self-labelling from
acceptance data rather than manual labelling.

What would *not* change: the architecture. The outbox, the graphs, the
guardrails, and the model registry are all independent of corpus size.

### Q: Sell me on this project. Why should I care?

It solves a real problem I watched happen: a support team raises the same bug
four times because nobody can find the ticket from four months ago, and four
developers fix it separately. The tracker already had the data — it just had no
way to read it.

What I would want you to take from it is not that I can call an embedding API.
It is that I made deliberate engineering decisions and can defend each one: why
hybrid retrieval rather than vector search alone, with an ablation table proving
it; why one Chroma collection per tenant rather than a metadata filter, and what
that protects against; why similar-issue search is not an agent and bug analysis
is; why the agent has no write tools at all.

And that I know how to tell whether it works. There is an evaluation harness with
retrieval metrics that gate every change, RAGAS on a fixed sample, and an
acceptance rate from real usage — because the offline numbers grade the
algorithm and only acceptance grades the product.

---

## Rapid-fire

Short answers to check you can define things precisely.

| Question | Answer |
|---|---|
| Cosine vs Euclidean for text? | Cosine — compares direction, ignores magnitude, which mostly tracks length |
| Why is `k=60` in RRF? | Convention (Cormack et al.); damps the top rank so one list cannot dominate. But we do not ship RRF — see below |
| Why did you drop RRF? | It scored below its own best retriever. An agreement bonus large enough to matter is large enough to displace the leader's top hit: safety requires `w ≤ 1/(k+2) ≈ 0.016`, at which point the secondary only orders the tail. That is a cascade, so we ship a cascade |
| What does `add_messages` do? | A reducer that appends rather than replaces, and dedupes by message id |
| Difference between retry and fallback? | Retry for transient 429s; fallback for exhausted quota, which retrying can never fix |
| What is HNSW? | A navigable small-world graph for approximate nearest-neighbour search. No training step, handles incremental inserts |
| Why `websearch_to_tsquery`? | It accepts what users type; `to_tsquery` raises on malformed input |
| What is faithfulness? | Whether every claim is supported by the retrieved context |
| What is MRR? | Mean reciprocal rank — sensitive to the position of the first correct answer |
| Why temperature 0 for reranking? | It is a judgement with a schema; randomness adds malformed output for no benefit |
| What makes an embedding model asymmetric? | Queries and documents are encoded differently, usually via an instruction prefix on the query |
| Why not text-to-SQL? | The model's input includes attacker-authored ticket text; generated SQL is an injection surface over the whole database |
| What does a checkpointer give you? | Durable graph state per thread — memory across requests and survival across restarts |
| Biggest cost lever in an agent? | Prompt caching the stable prefix, because history is resent every turn |
| Why does agent cost grow faster than turns? | Each turn resends all prior turns and tool results — roughly quadratic |
| One thing that makes injection survivable? | The agent has no write tools |

---

## Questions to ask them

Asking good questions signals seniority as much as answering does.

- How do you evaluate your LLM features? Do you have a golden set, and does it
  gate deploys?
- What does an LLM outage do to your product — degrade a feature, or take
  something down?
- Where do your guardrails live: prompts, code, or infrastructure? *(Prompt-only
  is a real answer, and tells you a lot.)*
- How do you handle multi-tenancy in your vector store?
- What is your cost per useful outcome, not per call?
- When did you last decide **not** to use an agent for something?

That last one is the best question on the list. A team that has never declined
an agent has probably built some they did not need.

---

## The things that actually went wrong

This is the most useful section in the file. Anyone can describe a RAG
pipeline; the questions that separate candidates are "what broke?" and "how did
you find out?". Every item below was found by running the system, not by
reading the code, and each one has a number attached.

### Retrieval

**The first benchmark scored 1.000 on everything.** The golden set paraphrased
only ticket *titles* and reused the base description across each cluster, so the
task was exact-text matching rather than paraphrase matching — which both
retrievers solve perfectly. A benchmark everything passes has no power to
distinguish anything. *Lesson: check for saturation before celebrating a score.*

**A tokenizer mismatch silently broke every error-code lookup.** Postgres
tokenizes `ERR-40001` into `'err'` and `'-40001'` — the hyphen stays with the
number. The Python-side query builder stripped hyphens and searched for
`40001`, which appears in no document. Keyword search looked "just weak at
identifiers"; it was returning nothing. Fixed by building the tsquery in SQL
from `to_tsvector`, so query and index are analysed by the same analyzer. R@5
on identifier queries went from 0.083 to 0.625, and R@20 from 0.583 to 1.000.

**RRF lost to its own inputs.** Equal-weight Reciprocal Rank Fusion scored
0.986 recall@5 where semantic alone scored 1.000, and 0.792 recall@20 where
keyword alone scored 1.000 — below the better retriever on both query families.
The reason is structural, not a misconfiguration: at K=60 a
document at rank 1 in one list scores the same as rank 1 in the other, so a
weak retriever's confident junk displaces a strong one's best answer.
Requiring that an agreement bonus never displace the leader's top hit gives
`w ≤ 1/(K+2) ≈ 0.016`, at which point the secondary only orders the tail —
which *is* a cascade. So "keep the agreement bonus" and "never rank worse than
the better retriever" are not simultaneously satisfiable, and the choice has to
be made rather than tuned around.

**Choosing a retriever per query made things worse.** "Does the query contain an
identifier?" is a real signal and the wrong use of it: the similar-issues query
*is* a ticket's own text, and every ticket carries a unique error code, so the
heuristic fired on prose queries and dropped duplicate recall to keyword
search's own score, because that is what the pipeline had become on every
query. The fix was to stop spending a fact as a hint — look identifiers up
exactly, pin the unique matches, and leave the ranking alone. Identifier
queries went to 1.000 recall at MRR 1.000.

### Models and cost

**The configured model names were dead.** `gemini-2.5-flash` returns
`404 — no longer available to new users` on a key issued now. Recalled model
names age badly. It was a two-line settings change because nothing else in the
codebase knows a model name.

**The cheap model reranked better than the strong one.** 0.963 against 0.912
precision@5 at a quarter of the latency (4.2s against 16.7s). The precision gap
is within noise on twelve queries; the latency gap is not. The assumption that
"classification is quality-sensitive so it needs the strong model" was wrong,
and measuring it cost one script run.

**Chain-of-thought had to be switched off on the local model.** `qwen3` is a
reasoning model: "Say OK" produced 9 output tokens with thinking off and 217
with it on. Output tokens dominate the cost of a rerank, so a call went from
timing out at 25s to finishing in 5s. It is a real trade — reasoning can help a
borderline case — but the answer is schema-constrained, so the thinking is
generated, paid for, and discarded.

**The local fallback was billed at the hosted rate.** The cost rate lived on the
*role*, so a call the local model served for free recorded about $0.005. Cost
per accepted suggestion is only meaningful if the costs in it are real.

### Agents and state

**`SET LOCAL app.tenant_id` is transaction-scoped.** The agent commits after
each step so progress can be polled, and every commit silently dropped the
row-level-security tenant scope. Two symptoms, one cause: tools reported
"ticket not found" for a ticket that plainly exists, and a step insert failed
with "new row violates row-level security policy". Neither symptom points at
the commit.

**`operator.add` cannot express "reset".** Per-turn chat state passed `[]` each
turn expecting a clear; `[]` appends nothing. Observations accumulated for the
life of the thread, so every turn carried every earlier question and cost more
than the last. Fixed with a sentinel-aware reducer — the same idiom
`add_messages` uses for `RemoveMessage`.

**LangGraph `isinstance`-checks its checkpointer.** A structurally complete
wrapper was rejected by `compile()`. "It has the right methods" is a guess
about a library's contract.

**`AsyncPostgresSaver` cannot run on Windows' default event loop**, because
psycopg's async mode refuses ProactorEventLoop. Wrapping the *sync* saver and
delegating to a worker thread was better than forcing the whole application
onto SelectorEventLoop for one component that writes once per turn.

**A crashed agent run blocked its ticket forever.** Deduplication was by status
alone, so a run left in `running` by a killed worker meant that ticket could
never be analysed again.

### Prompts and tools

**The tool contract was never transmitted.** The prompts listed parameter
*names* only, so every `Field(description=...)` was written, reviewed, and never
sent. The symptom looked like a model problem — the assistant said it "could not
break the data down by team" — and it was right, because it had no way to know a
`team` parameter existed. Two rounds of tool redesign went into that. *A tool
description is not developer documentation; it is the interface the model
programs against.*

**A tool result has to describe itself.** With the descriptions fixed, the
assistant *did* filter by team, got buckets keyed by status, and still answered
that the data "only shows counts by status, not by team" — because nothing in
the result said the filter had been applied. Results now echo
`filters_applied`.

**A silently discarded filter is worse than a rejected one.** An
injection-shaped status value was safely coerced away, and the query widened
from "closed" to the entire corpus with nothing telling the model.

**Gemini 3.x returns content blocks, not strings.** `str()` on them produced a
JSON-ish blob complete with a cryptographic signature field, shown to a user as
the assistant's reply.

### Safety

**A redaction that keeps the value is worse than none.** Against
`"Here is the token: Bearer sk-live-abc123..."` the credential pattern matched
`"token: Bearer"` — "Bearer" being six non-space characters — and left the
secret in the text. It looked like it had worked.

**"No tool may mutate" failed on `get_comments`**, because the check tested for
the substring "comment". A blocklist of nouns cannot tell reading a comment from
writing one. The rule is now inverted: every tool name must *begin* with a
reading verb.

### The meta-lesson

Five of the six retrieval evaluation runs were misleading in some way, and none
of them looked misleading. A saturated benchmark reads as success. A broken
tokenizer reads as "keyword search is weak here". A heuristic firing on the
wrong inputs reads as "fusion does not help". The metric was almost never the
thing that was wrong — the setup was. Which is why the harness reports
per-family results, prints its own conclusion, and ablates each component
rather than emitting a single number.
