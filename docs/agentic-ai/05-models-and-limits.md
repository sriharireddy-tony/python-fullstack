# 05 — Models & Rate Limits

## What it is

Every LLM call in the system goes through a **model registry**: application code
asks for a *role* (`rerank`, `agent`, `chat`, `evaluator`), and configuration
decides which provider and model serves it, with a fallback chain and a rate
limit attached.

No file outside the registry and its adapters ever names a model.

---

## Why we chose this

### The constraint that drove the design

The free Gemini tier limits are severe:

| Model | RPM | **RPD** |
|---|---|---|
| Gemini 2.5 Flash | 5 | **20 per day** |
| Gemini Flash Lite | 15 | **500 per day** |

**20 requests per day is a hard blocker.** An agent run makes 8–12 LLM calls, so
2.5 Flash allows roughly **two agent runs per day**, then nothing until tomorrow.
No amount of careful coding works around that.

Flash Lite at 500 RPD is workable: about 50 agent runs a day, or a few hundred
reranks.

So the architecture has to treat model choice as a **per-role, runtime,
configurable** decision — not a constant. Which happens to also be exactly what
good practice looks like, so the constraint pushed us somewhere we wanted to be.

### Why a role-based registry rather than "the model"

Three reasons, in order of how much they matter:

1. **Quota routing.** High-volume cheap work goes to Flash Lite; the few calls
   needing judgement go to 2.5 Flash. Impossible with one global model.
2. **Substitutability.** Development uses the free tier, production will use
   paid. Swapping is a config edit, not a code change.
3. **Testability.** Tests point every role at a fake model. No network, no key,
   no quota — the whole graph suite runs in CI.

### Why `qwen3:8b` as fallback

When Gemini quota runs out mid-development, everything stops. A local Ollama
model means it does not. `qwen3:8b` is already pulled, runs offline, and has no
limits.

**Honest caveat:** an 8B local model's tool-calling is noticeably less reliable
than Gemini's for a multi-turn agent loop — it more often malforms arguments or
loops. So the routing is deliberate:

| Role | Fallback to local? |
|---|---|
| `rerank`, `chat`, `query_rewrite` | Yes — quality dip is acceptable |
| `agent` | Yes, but flagged as degraded in the report |
| `evaluator` | **No.** A weak judge produces misleading metrics, which is worse than no metrics |

That last row matters. An evaluation you cannot trust is more dangerous than an
evaluation you do not have, because you will act on it.

### Alternatives considered

| Option | Why not |
|---|---|
| One model everywhere | 20 RPD makes it unusable |
| Paid tier now | Unnecessary in development; the free tier plus local fallback is sufficient |
| Local-only (`qwen3:8b` for everything) | Weaker judgement, and the goal includes using a hosted API properly |
| A hand-rolled provider wrapper | LangChain already provides the uniform interface, retries, and fallbacks |

---

## How it works technically

### The role table

As built, with model names **verified against the live API** rather than
assumed:

```
ROLE            PRIMARY                    FALLBACK    TEMP   DEADLINE  DAILY
────────────────────────────────────────────────────────────────────────────────
rerank          gemini-flash-lite-latest   qwen3:8b    0.0    25s       250
query_rewrite   gemini-flash-lite-latest   qwen3:8b    0.0    12s       100
chat            gemini-3.5-flash           qwen3:8b    0.0    30s       150
agent           gemini-3.5-flash           qwen3:8b    0.0    120s      100
evaluator       gemini-3.5-flash           (none)      0.0    30s       200
embedding       ollama qwen3-embedding     —           —      —         unlimited
```

Temperature is 0 everywhere, including chat. Every call in this system is
either a classification or a structured extraction, and a classification that
changes between identical runs cannot be cached, evaluated, or debugged.

Three things in that table were **corrections forced by measurement**, and each
one is the registry earning its keep:

**1. The configured model names were dead.** `gemini-2.5-flash` and
`gemini-2.5-flash-lite` both return `404 NOT_FOUND — "no longer available to
new users"` on a key issued today. Recalled model names age badly; the fix was
two settings lines because nothing else in the codebase knows a model name.

**2. The reranker uses the *cheap* model, and that was a surprise.** The role
was written on the assumption that classification is the quality-sensitive call
and deserves the strong model. Measured over twelve golden queries:

| configuration              | precision@5 | s/query |
|----------------------------|-------------|---------|
| retrieval only, no LLM     | 0.633       | 0.8     |
| `gemini-3.5-flash`         | 0.912       | 16.7    |
| `gemini-flash-lite-latest` | 0.963       | 4.2     |

The cheap model was better *and* four times faster. Re-running gave 0.929 for
the cheap model, so the precision difference between the two is within
run-to-run noise on a sample this size — but the latency difference is not, and
four seconds against seventeen is the difference between a panel that loads with
the page and one people watch a spinner for. So: cheap model, and the strong one
kept for the agent, where multi-step reasoning may genuinely need it.

**3. Deadlines are per role, not global.** A rerank happens while somebody
watches a page; the agent runs in a worker with nobody waiting. One global
timeout has to be wrong for one of them. The local 8B model takes ~20s for a
ten-candidate rerank, so a 25s interactive deadline lets it through on this
hardware while still cutting off a hang — and when it is cut off, the panel
degrades to the fused retrieval order rather than failing.

### One more measured surprise: turn off chain-of-thought

`qwen3` is a reasoning model. Left alone it emits a long `thinking` block before
answering, and the cost of a call here is dominated by **output** tokens.
Measured on this machine, the prompt "Say OK" produced:

| setting | output tokens |
|---|---|
| `think: false` | 9 |
| default | 217 |

For a ten-candidate rerank that ratio is the gap between 5 seconds and 30. It is
off by default, and the reasoning is specific rather than "faster is better":
the output is **schema-constrained**, so the model cannot reason inside its
answer, and the visible thinking is discarded. Paying twenty times the tokens
for text nobody reads is the wrong side of the trade — but it *is* a trade, and
`OLLAMA_THINKING` exists for the case where a hard classification matters more
than latency.

### Shape of the registry

```python
class ModelRegistry:
    """Roles in, configured chat models out.

    The only module that knows a model name. Everything else asks for a role,
    which is what makes provider swaps a config change and tests offline.
    """

    def get(self, role: str) -> BaseChatModel:
        spec = self._config[role]
        primary = self._build(spec.primary)
        chain = primary.with_retry(
            retry_if_exception_type=(RateLimitError,),
            wait_exponential_jitter=True,
            stop_after_attempt=3,
        )
        if spec.fallback:
            chain = chain.with_fallbacks([self._build(spec.fallback)])
        return chain
```

Two behaviours worth separating:

- **`with_retry`** handles a transient 429 — wait and try the same model again.
- **`with_fallbacks`** handles exhaustion — the daily quota is gone, retrying
  will never succeed, so switch models.

Conflating them produces a system that retries for three minutes against a limit
that resets tomorrow.

### Rate limiting before the call, not after

```python
await rate_limiter.acquire(role)     # token bucket, blocks until a slot frees
response = await model.ainvoke(messages)
```

5 RPM is one call every 12 seconds. Discovering that through 429s mid-agent-run
means the run dies half-finished. A token bucket paces the worker so the limit
is respected by construction.

### Response caching — the biggest development lever

```python
set_llm_cache(SQLiteCache(database_path="./var/llm_cache.db"))
```

Identical prompt → cached response, **zero API calls**. During development the
same graph is run dozens of times while tuning a downstream node; after the
first execution those calls are free.

Practical effect: a day of iteration that would exceed 500 RPD stays inside a
few dozen real calls.

*Caveat:* the cache is keyed on the exact prompt, so changing a single character
in the system prompt invalidates everything. That is correct, and worth knowing
when you wonder why the quota suddenly drains.

### Daily quota tracking

The registry cannot see Google's counter, so it keeps its own:

```
ai_usage: (tenant_id, role, model, input_tokens, output_tokens,
           estimated_cost, latency_ms, created_at)
```

A pre-flight check sums today's calls per model and refuses — or falls back —
before hitting the provider's limit. This is also what makes per-tenant budgets
possible later, and it turns "the AI features cost too much" from an argument
into a query.

### Handling the local model's quirks

`qwen3:8b` is a reasoning model and emits thinking blocks:

```python
ChatOllama(model="qwen3:8b", temperature=0, reasoning=False)
# plus a strip pass, because the flag is not always honoured
```

Left unhandled, `<think>…</think>` ends up inside the JSON we try to parse.

---

## Worked scenario

**A day of development against the free tier.**

```
09:00  Start work. Run the similarity graph on OS-193.
       → rerank: 1 real call to Flash Lite.        used 1/500

09:05  Tweak the threshold in the grading node, re-run.
       → identical rerank prompt → SQLite cache hit, 0 calls.   1/500

09:40  Change the rerank prompt wording. Re-run 8 times while tuning.
       → 8 real calls (prompt changed, cache misses).           9/500

11:00  Run the Tier-1 eval harness over 40 golden pairs.
       → 0 calls. Retrieval metrics need no LLM.                9/500

14:00  Run 3 agent analyses.
       → agent role = 2.5 Flash, 10 calls each = 30 calls.
       → 2.5 Flash daily limit is 20 → first 2 runs succeed,
         third exhausts it.
       → with_fallbacks routes run 3 to qwen3:8b, locally.
         Report is marked "produced by fallback model".

16:00  RAGAS on a 30-sample set, 3 judge calls each = 90 calls
       → evaluator role = Flash Lite.                          99/500

End of day: 99 of 500 on Flash Lite, 20 of 20 on 2.5 Flash,
            unlimited local calls. Nothing was blocked.
```

Without caching, the 09:40 tuning session alone would have been ~40 calls, and
the eval would have doubled it.

**Production, same code:**

```
LLM_PROVIDER=google
GEMINI_MODEL_STRONG=gemini-3.5-flash      # agent and chat
GEMINI_MODEL_CHEAP=gemini-flash-lite-latest   # rerank and rewrite
LLM_FALLBACK_ENABLED=false            # no local model in production
```

Config only. No code change.

---

## Interview questions

**Q: How do you handle rate limits and quota with a hosted LLM?**

Four layers, because one is never enough. A token-bucket limiter paces requests
so the per-minute limit is respected before a call is made rather than
discovered through 429s. Retry with exponential backoff for transient limits.
Fallback to a different model for *exhaustion*, which retrying can never fix.
And response caching, so repeated identical prompts during development cost
nothing. The distinction between retry and fallback matters most — a daily quota
resets tomorrow, so retrying against it just wastes three minutes and still
fails.

**Q: How do you decide which model to use for which task?**

By what the task actually needs, then by cost. Structured classification like
reranking or query rewriting does not need deep reasoning, so it runs on the
cheap fast model at temperature zero. Multi-turn agent reasoning across a dozen
documents does need judgement, so it gets the stronger model. And the evaluator
gets a capable model deliberately, because a weak judge produces misleading
metrics — which is worse than having no metrics, since you will act on them. All
of that lives in a registry keyed by role, so it is configuration rather than
scattered conditionals.

**Q: Your LLM provider goes down in production. What happens?**

The AI features degrade; nothing else does. Retrieval still runs — it needs no
LLM at all — so similar issues still appear, just unranked and unexplained. The
agent returns a clean failure with a retry option rather than a half-written
report. Ticket creation, workflow, and search are untouched, because no AI call
sits on a write path. That is deliberate: an AI dependency being down should
degrade an AI feature, not the product.

**Q: How do you test code that calls an LLM?**

By making the model a dependency rather than an import. Every role resolves
through the registry, so tests point them at a fake model returning canned
responses. That means the graph tests assert on *which path was taken* and how
state changed — deterministic, no network, no API key, no quota. Then a small
number of contract tests exercise the real adapters. The point is that
non-determinism is confined to the adapter boundary, so everything above it is
ordinary testable code.

**Q: How do you track and control cost?**

Every call writes a row recording role, model, token counts, latency, and
estimated cost. That gives per-tenant and per-feature attribution, so "the AI
features cost too much" becomes a query rather than an argument. On top of it: a
per-run cost cap checked between agent turns, a per-tenant monthly budget
checked before a run starts, and prompt caching on the stable prefix. Because
agent history is resent every turn, caching the tool schemas and system prompt is
the single largest lever — roughly halving cost per run.

**Q: Why not just use a local model for everything?**

We do use one for embeddings, where it is genuinely the right answer, and as a
fallback everywhere else. But an 8B local model's multi-turn tool-calling is
noticeably less reliable than a hosted frontier model — malformed arguments,
loops that do not converge. For a ReAct agent making ten dependent tool calls
that unreliability compounds. So local is the fallback for chat and reranking,
where a quality dip is acceptable, and explicitly *not* the fallback for
evaluation, where a weak judge would quietly corrupt the metrics we make
decisions from.

---

## Gotchas

- **20 RPD is per day, not per minute.** Plan the agent's model accordingly.
- **Retry and fallback are different mechanisms.** Retrying an exhausted daily
  quota is pure waste.
- **The LLM cache is keyed on the exact prompt.** One character in the system
  prompt invalidates every entry — expect the quota to drain after a prompt edit.
- **`qwen3:8b` emits `<think>` blocks.** Disable reasoning *and* strip, because
  the flag is not always honoured.
- **Ollama's first call after idle is slow** while the model loads. Warm it at
  startup.
- **Never let the evaluator fall back to a weaker model.** Fail loudly instead —
  metrics you cannot trust are worse than none.
- **Temperature 0 for anything with a schema.** Non-zero temperature on
  structured output increases malformed responses for no benefit.
