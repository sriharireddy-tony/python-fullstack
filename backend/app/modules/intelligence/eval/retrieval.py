"""Tier-1 retrieval metrics.

No LLM, no API key, no cost, no randomness. That combination is what makes
this the gate that runs on every retrieval change — an evaluation you cannot
afford to run is an evaluation you will not run.

## The three metrics, and what each one is for

**recall@k** — of the documents that *should* have been found, how many made
it into the top k? This is the ceiling on everything downstream. A reranker
can reorder the candidate list; it cannot conjure a document retrieval never
returned. If ``recall@20`` is poor, no amount of prompt work in Phase C will
save the feature, and that is the number to fix first.

**MRR** — one over the rank of the first correct answer. The user-experience
metric: it answers "how far down did the reader have to look before finding
something useful". MRR ignores everything after the first hit, deliberately, so
it is the right measure for a panel showing five results where finding one good
one is the whole job.

**nDCG@k** — discounted cumulative gain, normalised by the best achievable
ordering. Unlike MRR it cares about *all* the relevant documents and where each
one landed, and unlike recall it is sensitive to order. It is the metric that
notices "we found all three duplicates but ranked them 4th, 5th and 6th, below
three irrelevant ones" — which recall@10 reports as perfect.

They disagree in useful ways, which is exactly why all three are reported. A
change that improves recall while hurting nDCG has found more documents and
ranked them worse; that is a real trade-off and not something to average away
into one score.
"""

from __future__ import annotations

import math
import uuid
from collections.abc import Sequence
from dataclasses import dataclass, field

#: Cutoffs reported by default. 5 is what the panel shows, 10 is one scroll,
#: 20 is the candidate pool a Phase-C reranker gets to work with — so
#: ``recall@20`` is the honest ceiling on the finished feature.
DEFAULT_CUTOFFS = (1, 3, 5, 10, 20)


def recall_at_k(retrieved: Sequence[uuid.UUID], relevant: frozenset[uuid.UUID], k: int) -> float:
    if not relevant:
        return 0.0
    hits = sum(1 for ticket_id in retrieved[:k] if ticket_id in relevant)
    return hits / len(relevant)


def precision_at_k(retrieved: Sequence[uuid.UUID], relevant: frozenset[uuid.UUID], k: int) -> float:
    """Precision with the denominator capped at what was actually returned.

    Dividing by ``k`` when only three results came back would punish a query
    for the corpus being small, which measures the dataset rather than the
    retriever.
    """
    window = retrieved[:k]
    if not window:
        return 0.0
    return sum(1 for ticket_id in window if ticket_id in relevant) / len(window)


def reciprocal_rank(retrieved: Sequence[uuid.UUID], relevant: frozenset[uuid.UUID]) -> float:
    for position, ticket_id in enumerate(retrieved, start=1):
        if ticket_id in relevant:
            return 1.0 / position
    return 0.0


def ndcg_at_k(retrieved: Sequence[uuid.UUID], relevant: frozenset[uuid.UUID], k: int) -> float:
    """Binary-relevance nDCG.

    Gain is 1 for a relevant document and 0 otherwise — we have no graded
    labels, and inventing a "somewhat relevant" tier from planted data would be
    dressing up a guess as ground truth.

    The ideal DCG is computed over ``min(len(relevant), k)`` documents, so a
    query with more duplicates than the cutoff is not penalised for the cutoff.
    """
    if not relevant:
        return 0.0
    dcg = sum(
        1.0 / math.log2(position + 1)
        for position, ticket_id in enumerate(retrieved[:k], start=1)
        if ticket_id in relevant
    )
    ideal = sum(1.0 / math.log2(position + 1) for position in range(1, min(len(relevant), k) + 1))
    return dcg / ideal if ideal else 0.0


@dataclass(slots=True)
class RetrievalScores:
    """Averaged metrics for one configuration over the whole query set."""

    label: str
    queries: int
    recall: dict[int, float] = field(default_factory=dict)
    precision: dict[int, float] = field(default_factory=dict)
    ndcg: dict[int, float] = field(default_factory=dict)
    mrr: float = 0.0
    #: Queries where nothing relevant was found at any depth. More actionable
    #: than an average: an average of 0.8 hides whether the remaining 0.2 is
    #: spread thinly or is five queries that failed completely.
    total_misses: int = 0
    elapsed_seconds: float = 0.0

    @property
    def per_query_ms(self) -> float:
        return (self.elapsed_seconds / self.queries * 1000) if self.queries else 0.0


def score(
    label: str,
    outcomes: list[tuple[Sequence[uuid.UUID], frozenset[uuid.UUID]]],
    *,
    cutoffs: tuple[int, ...] = DEFAULT_CUTOFFS,
    elapsed_seconds: float = 0.0,
) -> RetrievalScores:
    """Average the metrics over every query.

    A macro average — each query counts once, regardless of how many relevant
    documents it has. Micro-averaging would let the four-member payslip cluster
    outvote the two-member ones and quietly turn this into a measure of one
    scenario.
    """
    count = len(outcomes)
    result = RetrievalScores(label=label, queries=count, elapsed_seconds=elapsed_seconds)
    if count == 0:
        return result

    for k in cutoffs:
        result.recall[k] = sum(recall_at_k(r, rel, k) for r, rel in outcomes) / count
        result.precision[k] = sum(precision_at_k(r, rel, k) for r, rel in outcomes) / count
        result.ndcg[k] = sum(ndcg_at_k(r, rel, k) for r, rel in outcomes) / count

    result.mrr = sum(reciprocal_rank(r, rel) for r, rel in outcomes) / count
    result.total_misses = sum(1 for r, rel in outcomes if not (set(r) & rel))
    return result
