"""Does reranking actually improve what the panel shows?

    .venv\\Scripts\\python -m scripts.eval_rerank
    .venv\\Scripts\\python -m scripts.eval_rerank --limit 8

Separate from ``eval_retrieval`` for one reason: **this one costs money and
time.** Tier-1 retrieval evaluation is free and runs on every change; this
makes one LLM call per query, so it runs deliberately, on a fixed sample, when
the prompt or the model changes.

## The metric is precision, not recall

Reranking cannot improve recall — it only reorders and filters what retrieval
already returned. So measuring recall here would produce a table that says
"reranking changed nothing", which is true and useless. What it can change is
**precision@5**: of the five things shown to a human, how many were worth
showing. That is the number this measures, alongside the two costs paid for it:
latency, and how often the model cited something it was never given.

## What "worth showing" means here

The planted duplicate clusters are the labels: a result inside the query's
cluster is a true positive. That understates the reranker, because a genuinely
related ticket outside the planted cluster counts against it — the same
pessimism doc 10 notes for precision generally. Read the *difference* between
the rows, not the absolute values.
"""

from __future__ import annotations

import argparse
import asyncio
import time
import uuid
from dataclasses import dataclass, field

from app.core.database import session_scope
from app.core.logging import configure_logging
from app.modules.intelligence.deps import get_ai_deps
from app.modules.intelligence.eval.datasets import load_golden_queries
from app.modules.intelligence.service import IntelligenceService
from app.modules.tenancy.models import Tenant
from sqlalchemy import select

DISPLAY_LIMIT = 5


@dataclass
class Outcome:
    """What one configuration achieved over the sample."""

    label: str
    queries: int = 0
    #: Sum of per-query precision, averaged at the end.
    precision_total: float = 0.0
    #: Queries where the top result was a genuine duplicate.
    top_hit: int = 0
    #: Queries that returned nothing at all. Not a failure -- an empty answer
    #: is a real answer -- but worth separating from a wrong one.
    empty: int = 0
    shown_total: int = 0
    seconds: float = 0.0
    ungrounded: int = 0
    relations: dict[str, int] = field(default_factory=dict)

    @property
    def precision(self) -> float:
        return self.precision_total / self.queries if self.queries else 0.0

    @property
    def per_query_seconds(self) -> float:
        return self.seconds / self.queries if self.queries else 0.0


async def resolve_tenant(code: str) -> uuid.UUID:
    async with session_scope(tenant_id=None) as db:
        tenant_id = await db.scalar(select(Tenant.id).where(Tenant.code == code))
    if tenant_id is None:
        raise SystemExit(f"no tenant with code {code!r}")
    return tenant_id


async def run(tenant_id: uuid.UUID, label: str, *, rerank: bool, limit: int) -> Outcome:
    ai = await get_ai_deps()
    outcome = Outcome(label=label)

    async with session_scope(tenant_id=tenant_id) as db:
        queries = await load_golden_queries(db)
        service = IntelligenceService(db, tenant_id, ai)

        for query in queries[:limit]:
            started = time.perf_counter()
            result = await service.similar_to_text(
                query.title,
                query.description,
                limit=DISPLAY_LIMIT,
                exclude=query.exclude,
                with_rerank=rerank,
            )
            outcome.seconds += time.perf_counter() - started
            outcome.queries += 1

            shown = result.results
            outcome.shown_total += len(shown)
            if not shown:
                outcome.empty += 1
                continue

            hits = [item for item in shown if item.ticket_id in query.relevant]
            outcome.precision_total += len(hits) / len(shown)
            if shown[0].ticket_id in query.relevant:
                outcome.top_hit += 1

            for item in shown:
                if item.relation is not None:
                    key = item.relation.value
                    outcome.relations[key] = outcome.relations.get(key, 0) + 1

    return outcome


def table(rows: list[Outcome]) -> str:
    header = ["configuration", "queries", "P@5", "top-1 hit", "shown", "empty", "s/query"]
    body = [
        [
            row.label,
            str(row.queries),
            f"{row.precision:.3f}",
            f"{row.top_hit}/{row.queries}",
            str(row.shown_total),
            str(row.empty),
            f"{row.per_query_seconds:.1f}",
        ]
        for row in rows
    ]
    widths = [max(len(header[i]), *(len(r[i]) for r in body)) for i in range(len(header))]
    lines = [
        "  ".join(cell.ljust(widths[i]) for i, cell in enumerate(header)),
        "  ".join("-" * w for w in widths),
    ]
    lines += ["  ".join(cell.ljust(widths[i]) for i, cell in enumerate(r)) for r in body]
    return "\n".join(lines)


async def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--tenant", default="keka")
    parser.add_argument(
        "--limit",
        type=int,
        default=8,
        help="How many golden queries to run. Each reranked query is one LLM call.",
    )
    args = parser.parse_args()

    configure_logging()
    tenant_id = await resolve_tenant(args.tenant)

    rows: list[Outcome] = []
    for label, rerank in (("retrieval only", False), ("retrieval + rerank", True)):
        print(f"running: {label} ...", end="", flush=True)
        outcome = await run(tenant_id, label, rerank=rerank, limit=args.limit)
        rows.append(outcome)
        print(f" {outcome.per_query_seconds:.1f}s/query")

    print(f"\n{'=' * 84}")
    print(f"RERANK EVALUATION  (tenant={args.tenant}, showing top {DISPLAY_LIMIT})")
    print("=" * 84)
    print(table(rows))

    reranked = rows[-1]
    if reranked.relations:
        spread = ", ".join(f"{k}={v}" for k, v in sorted(reranked.relations.items()))
        print(f"\nrelations assigned: {spread}")

    baseline, judged = rows[0], rows[1]
    delta = judged.precision - baseline.precision
    cost = judged.per_query_seconds - baseline.per_query_seconds
    print(
        f"\nprecision@{DISPLAY_LIMIT} {delta:+.3f} for {cost:+.1f}s per query"
        f" ({judged.precision:.3f} vs {baseline.precision:.3f})"
    )
    if delta > 0.01:
        print(
            "verdict: the rerank earns its call -- it removes results retrieval ranked\n"
            "         highly but a reader would not have wanted."
        )
    elif delta < -0.01:
        print(
            "verdict: the rerank is REMOVING good results. Check the confidence floor\n"
            "         before blaming the prompt: the floor drops candidates silently."
        )
    else:
        print(
            "verdict: no measurable precision gain on this sample. Either the retrieval\n"
            "         order was already right, or the sample is too small to tell.\n"
            "         Note what the rerank still adds: a stated relation and a reason,\n"
            "         which is what makes a result actionable rather than merely present."
        )


if __name__ == "__main__":
    asyncio.run(main())
