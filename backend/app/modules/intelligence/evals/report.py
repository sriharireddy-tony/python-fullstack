"""Turning scores into a table someone will actually read, and a conclusion.

Kept out of the script so the formatting can be reused by the Phase-G
dashboard, and so the script stays about orchestration.
"""

from __future__ import annotations

from dataclasses import dataclass

from app.modules.intelligence.evals.retrieval import RetrievalScores

#: The configuration that actually ships. Named here so the verdict compares
#: the right row — an ablation that grades a configuration nobody runs is a
#: table, not a decision.
SHIPPED = "fusion (semantic + lexical)"

#: Recall differences below this are noise on a set this small. Nineteen
#: queries means one query is worth ~0.05, so a 0.005 "improvement" is not one.
TOLERANCE = 0.005


def ablation_table(runs: list[RetrievalScores], cutoffs: tuple[int, ...]) -> str:
    """One row per configuration, one column per metric.

    Read down the ``R@20`` column first: that is the ceiling on the finished
    feature, because it bounds what any reranker can possibly return.
    """
    header = (
        ["configuration", "queries", "MRR"]
        + [f"R@{k}" for k in cutoffs]
        + [f"nDCG@{k}" for k in cutoffs]
        + ["misses", "ms/query"]
    )
    rows = [
        [
            run.label,
            str(run.queries),
            f"{run.mrr:.3f}",
            *[f"{run.recall.get(k, 0.0):.3f}" for k in cutoffs],
            *[f"{run.ndcg.get(k, 0.0):.3f}" for k in cutoffs],
            str(run.total_misses),
            f"{run.per_query_ms:.0f}",
        ]
        for run in runs
    ]

    widths = [max(len(header[i]), *(len(row[i]) for row in rows)) for i in range(len(header))]
    lines = [
        "  ".join(cell.ljust(widths[i]) for i, cell in enumerate(header)),
        "  ".join("-" * width for width in widths),
    ]
    lines += ["  ".join(cell.ljust(widths[i]) for i, cell in enumerate(row)) for row in rows]
    return "\n".join(lines)


@dataclass(frozen=True, slots=True)
class FamilyVerdict:
    """What one query family concluded."""

    family: str
    #: Which single retriever won this family, and by how much over the other.
    best_single: str
    best_single_recall: float
    shipped_recall: float
    shipped_mrr: float
    #: True when the shipped combiner is at least as good as the best single
    #: retriever, within tolerance.
    matches_best: bool
    #: Which retriever wins this family *unaided* -- probe off. This is the row
    #: that answers "does each retriever earn its place", because with the
    #: exact probe on, both answer identifier queries perfectly and the
    #: comparison becomes a tie broken by nothing.
    unaided_winner: str | None = None
    unaided_margin: float = 0.0

    def line(self, at: int) -> str:
        relation = (
            "matches" if abs(self.shipped_recall - self.best_single_recall) <= TOLERANCE else None
        )
        if relation is None:
            relation = "beats" if self.shipped_recall > self.best_single_recall else "LOSES to"
        return (
            f"{self.family:<12} shipped fusion {relation} the best single retriever "
            f"({self.best_single}): R@{at} {self.shipped_recall:.3f} vs "
            f"{self.best_single_recall:.3f}, MRR {self.shipped_mrr:.3f}"
        )


def judge(family: str, runs: list[RetrievalScores], *, at: int = 20) -> FamilyVerdict | None:
    """Compare the shipped combiner against each retriever on its own."""
    by_label = {run.label: run for run in runs}
    shipped = by_label.get(SHIPPED)
    singles = [run for label, run in by_label.items() if label.endswith(" only")]
    if shipped is None or not singles:
        return None

    best = max(singles, key=lambda run: (run.recall.get(at, 0.0), run.mrr))
    shipped_recall = shipped.recall.get(at, 0.0)
    best_recall = best.recall.get(at, 0.0)

    unaided = sorted(
        (run for label, run in by_label.items() if label.endswith(", no probe") and "," in label),
        key=lambda run: (run.recall.get(at, 0.0), run.mrr),
        reverse=True,
    )
    unaided_singles = [run for run in unaided if not run.label.startswith("fusion")]

    return FamilyVerdict(
        family=family,
        best_single=best.label,
        best_single_recall=best_recall,
        shipped_recall=shipped_recall,
        shipped_mrr=shipped.mrr,
        matches_best=shipped_recall >= best_recall - TOLERANCE,
        unaided_winner=unaided_singles[0].label if unaided_singles else None,
        unaided_margin=(
            unaided_singles[0].recall.get(at, 0.0) - unaided_singles[-1].recall.get(at, 0.0)
            if len(unaided_singles) > 1
            else 0.0
        ),
    )


def conclusion(verdicts: list[FamilyVerdict], *, at: int = 20) -> str:
    """The decision the whole ablation exists to support.

    Three outcomes, and only one of them justifies shipping both retrievers:

    * **One retriever wins every family** — then the other is latency for
      nothing and should be deleted. The roadmap's rule for this phase.
    * **Different retrievers win different families** — then both are needed,
      and the only question left is whether the combiner gives away what the
      winner earned.
    * **The combiner is worse than the best single retriever somewhere** — then
      the combiner is wrong, whatever the retrievers do.
    """
    if not verdicts:
        return "no families ran; nothing to conclude"

    # Winners are read from the *unaided* rows where they exist: with the exact
    # probe on, both retrievers answer identifier queries perfectly, so the
    # probe-assisted comparison is a tie and says nothing about the retrievers.
    winners = {
        verdict.unaided_winner or verdict.best_single
        for verdict in verdicts
        if verdict.unaided_margin > TOLERANCE or verdict.unaided_winner is None
    }
    regressions = [verdict for verdict in verdicts if not verdict.matches_best]

    if regressions:
        detail = ", ".join(
            f"{verdict.family} ({verdict.shipped_recall:.3f} vs {verdict.best_single_recall:.3f})"
            for verdict in regressions
        )
        return (
            f"FIX THE COMBINER — shipped fusion is below the best single retriever on: {detail}. "
            "A combiner that gives away what a retriever earned is worse than no combiner."
        )

    if len(winners) == 1:
        only = next(iter(winners))
        return (
            f"DROP THE OTHER RETRIEVER — {only} wins every family, and fusion only matches it. "
            "Two retrievers are not worth the latency when one is never better."
        )

    listed = " and ".join(sorted(winners))
    margins = ", ".join(
        f"{verdict.family} +{verdict.unaided_margin:.3f} to {verdict.unaided_winner}"
        for verdict in verdicts
        if verdict.unaided_winner and verdict.unaided_margin > TOLERANCE
    )
    return (
        f"KEEP BOTH — unaided, no single retriever wins everywhere ({listed} each win a "
        f"family: {margins}), and shipped fusion matches the winner on every family at "
        f"R@{at}. That is the case for a hybrid: not that fusion beats its parts on "
        "average, but that it never loses to the better part while which part is better "
        "keeps changing."
    )
