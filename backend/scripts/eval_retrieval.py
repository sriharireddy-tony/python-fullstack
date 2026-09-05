"""Retrieval evaluation and ablation.

    .venv\\Scripts\\python -m scripts.eval_retrieval
    .venv\\Scripts\\python -m scripts.eval_retrieval --tenant keka --k 60

Runs the real pipeline — real embeddings, real Pinecone, real Postgres — once per
configuration over the planted golden set, and prints the ablation table.

The ablation is the point. "We built hybrid retrieval" is a claim; a table
showing what happens when each half is removed is evidence. And if the table
says one retriever alone is better, the right response is to delete the other,
not to explain the table away.

Zero LLM calls, so this is free to run and safe to run on every change.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
import time
import uuid
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass

from app.core.config import settings
from app.core.database import session_scope
from app.core.logging import configure_logging
from app.modules.intelligence.deps import get_ai_deps
from app.modules.intelligence.eval.datasets import (
    EvalQuery,
    load_golden_queries,
    load_identifier_queries,
)
from app.modules.intelligence.eval.report import (
    FamilyVerdict,
    ablation_table,
    conclusion,
    judge,
)
from app.modules.intelligence.eval.retrieval import RetrievalScores, score
from app.modules.intelligence.schemas.types import FusionMode, RetrievalSource
from app.modules.intelligence.service import IntelligenceService
from app.modules.tenancy.models import Tenant
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

CUTOFFS = (1, 3, 5, 10, 20)

#: The three configurations that answer "does each half earn its place".
#: Written as data so adding a fourth (a reranked run, in Phase C) is one line.
BOTH = frozenset({RetrievalSource.SEMANTIC, RetrievalSource.LEXICAL})
ONE_ONLY = FusionMode.CASCADE  # irrelevant with a single retriever

LEXICAL = frozenset({RetrievalSource.LEXICAL})
SEMANTIC = frozenset({RetrievalSource.SEMANTIC})


@dataclass(frozen=True, slots=True)
class Configuration:
    """One row of the ablation: which components are switched on."""

    label: str
    sources: frozenset[RetrievalSource]
    mode: FusionMode
    #: Off for the rows that isolate what the retrievers do unaided. With the
    #: exact probe on, every configuration answers identifier queries perfectly
    #: and the table stops telling you anything about the retrievers.
    probe: bool = True


#: Every configuration runs through the *shipped* code path, selected by
#: settings. An ablation with its own copy of the algorithm measures the copy,
#: and the two drift apart the first time one is edited.
CONFIGURATIONS: list[Configuration] = [
    # Retrievers unaided -- what each one can find on its own.
    Configuration("lexical, no probe", LEXICAL, ONE_ONLY, probe=False),
    Configuration("semantic, no probe", SEMANTIC, ONE_ONLY, probe=False),
    Configuration("fusion, no probe", BOTH, FusionMode.CASCADE, probe=False),
    # RRF unaided, so the "does fusion beat its own parts" question has an
    # apples-to-apples answer on *both* families. With the probe on, RRF scores
    # 1.000 on identifiers because the probe answered, not because RRF did --
    # which is how the comparison gets read the wrong way round.
    Configuration("rrf, no probe", BOTH, FusionMode.RRF, probe=False),
    # The full pipeline, one component at a time.
    Configuration("lexical only", LEXICAL, ONE_ONLY),
    Configuration("semantic only", SEMANTIC, ONE_ONLY),
    Configuration("rrf (equal weight)", BOTH, FusionMode.RRF),
    Configuration("fusion (semantic + lexical)", BOTH, FusionMode.CASCADE),
]

#: Retrieval depth for the evaluation. Must be at least the largest cutoff, or
#: recall@20 would be measuring the depth limit instead of the retriever.
DEPTH = 20

QueryLoader = Callable[[AsyncSession], Awaitable[list[EvalQuery]]]

#: The two query families, and what each one is for.
#:
#: Running only the first would have led to the wrong architectural decision:
#: it measures paraphrase recall, embeddings win it outright, and keyword
#: search looks like pure overhead. The second family tests the case keyword
#: search exists for. A single-family benchmark answers a narrower question
#: than the one being asked.
FAMILIES: list[tuple[str, str, QueryLoader]] = [
    (
        "duplicates",
        "planted near-duplicate clusters -- paraphrase recall",
        load_golden_queries,
    ),
    (
        "identifiers",
        "an error code pasted from a log -- exact-token recall",
        load_identifier_queries,
    ),
]


async def resolve_tenant(code: str) -> uuid.UUID:
    async with session_scope(tenant_id=None) as db:
        tenant_id = await db.scalar(select(Tenant.id).where(Tenant.code == code))
    if tenant_id is None:
        raise SystemExit(f"no tenant with code {code!r}")
    return tenant_id


async def run_configuration(
    tenant_id: uuid.UUID,
    rrf_k: int,
    loader: QueryLoader,
    config: Configuration,
) -> RetrievalScores:
    ai = await get_ai_deps()
    started = time.perf_counter()
    outcomes: list[tuple[Sequence[uuid.UUID], frozenset[uuid.UUID]]] = []

    # One session for the whole configuration. The lexical retriever is bound
    # to it, and row-level security scopes it to this tenant.
    async with session_scope(tenant_id=tenant_id) as db:
        queries = await loader(db)
        service = IntelligenceService(db, tenant_id, ai)
        for query in queries:
            retrieved = await service.candidate_ids(
                query.title,
                query.description,
                limit=DEPTH,
                exclude=query.exclude,
                sources=config.sources,
                rrf_k=rrf_k,
                fusion_mode=config.mode,
                exact_probe=config.probe,
            )
            outcomes.append((retrieved, query.relevant))

    elapsed = time.perf_counter() - started
    return score(config.label, outcomes, cutoffs=CUTOFFS, elapsed_seconds=elapsed)


async def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--tenant", default="keka", help="tenant code (default: keka)")
    parser.add_argument(
        "--k",
        type=int,
        default=None,
        help="RRF k. Omit to use the configured value; vary it only to observe, not to tune.",
    )
    args = parser.parse_args()

    configure_logging()
    tenant_id = await resolve_tenant(args.tenant)

    async with session_scope(tenant_id=tenant_id) as db:
        golden = await load_golden_queries(db)
    if not golden:
        raise SystemExit(
            "no golden queries found. Run `python -m scripts.seed_demo --reset` first — "
            "the golden set is the planted duplicate clusters."
        )

    clusters = sorted({query.cluster for query in golden})
    print(f"\nduplicate clusters: {len(golden)} queries across {len(clusters)} clusters")
    for cluster in clusters:
        members = [q for q in golden if q.cluster == cluster]
        print(f"  {len(members):>2} tickets  {cluster[:64]}")

    rrf_k = args.k or settings.RRF_K
    verdicts: list[FamilyVerdict] = []

    for family, blurb, loader in FAMILIES:
        async with session_scope(tenant_id=tenant_id) as db:
            family_queries = await loader(db)
        if not family_queries:
            print(f"\nskipping family {family!r}: no queries found")
            continue

        runs: list[RetrievalScores] = []
        for config in CONFIGURATIONS:
            print(f"\nrunning: {family}/{config.label} ...", end="", flush=True)
            run = await run_configuration(tenant_id, rrf_k, loader, config)
            runs.append(run)
            print(f" {run.per_query_ms:.0f} ms/query")

        print(f"\n{'=' * 100}")
        print(f"FAMILY: {family}  ({len(family_queries)} queries) -- {blurb}")
        print(f"tenant={args.tenant}, RRF k={rrf_k}, retrieval depth={DEPTH}")
        print("=" * 100)
        print(ablation_table(runs, CUTOFFS))
        print()
        for cutoff in (20, 5):
            outcome = judge(family, runs, at=cutoff)
            if outcome is None:
                continue
            print(outcome.line(cutoff))
            if cutoff == 20:
                verdicts.append(outcome)

    print(f"\n{'=' * 100}")
    print("SUMMARY")
    print("=" * 100)
    for outcome in verdicts:
        print(f"  {outcome.line(20)}")
    print()
    print(f"  {conclusion(verdicts, at=20)}")
    print(
        "\nnote: labels are planted by the seed and written by the same author as the "
        "system, so read these numbers comparatively, not as production accuracy."
    )

    # Non-zero exit when the shipped combiner is worse than a plain single
    # retriever anywhere. That is a regression a human must look at, and it is
    # the one outcome worth failing a build over -- "drop a retriever" is a
    # design decision, not a broken build.
    if any(not outcome.matches_best for outcome in verdicts):
        sys.exit(2)


if __name__ == "__main__":
    asyncio.run(main())
