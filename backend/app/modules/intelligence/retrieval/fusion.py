"""Reciprocal Rank Fusion — a pure function.

## Why rank fusion rather than score blending

The obvious way to combine two retrievers is a weighted sum of their scores::

    final = 0.7 * cosine_similarity + 0.3 * ts_rank_cd

It does not work, for three reasons that all bite in production:

1. **The scales are unrelated.** Cosine similarity over Qwen3 embeddings
   clusters tightly around 0.75-0.85 for anything plausible, while
   ``ts_rank_cd`` is unbounded and typically 0.01-0.3. Adding them means the
   cosine term decides everything and the weight is theatre.
2. **The distributions shift per query.** A rare term gives high lexical
   scores; a common one gives low ones. So a weight tuned on one query is
   wrong on the next, and no single pair of weights is right.
3. **Missing values have no sane default.** If lexical search did not return a
   document, is its lexical score 0? That would punish a document semantic
   search loved, purely for being absent from the other list.

RRF sidesteps all three by throwing the scores away and keeping only the
ranks::

    score(d) = Σ over retrievers r that returned d:  1 / (k + rank_r(d))

Ranks are comparable across retrievers by construction, they are stable under
any monotonic rescaling of the underlying scores, and absence is handled by
simply having no term in the sum. There is nothing to tune per corpus.

## What K does

``K`` (60 by default, from the original Cormack et al. paper) flattens the
curve near the top:

| rank | 1/(60+rank) | 1/(1+rank) |
|------|-------------|------------|
| 1    | 0.0164      | 0.5000     |
| 2    | 0.0161      | 0.3333     |
| 10   | 0.0143      | 0.0909     |

With a small K, rank 1 dominates so completely that a document at rank 1 in
one list always beats a document at rank 2 in *both*. With K=60 the second
case wins — which is the behaviour we want, because agreement between two
independent retrievers is stronger evidence than one retriever's confidence.

K=60 is not magic and is not tuned here on purpose: the value is a stated,
sourced default, and the ablation script measures whether fusion beats its
parts. Tuning K on a 220-ticket planted golden set would be overfitting with
extra steps.

## When equal weights are wrong

Plain RRF assumes the retrievers are of *comparable* quality for the query at
hand. Measured on this corpus, they are not, and not in a fixed direction:

| query family                       | semantic R@20 | lexical R@20 |
|------------------------------------|---------------|--------------|
| paraphrased duplicates             | 1.000         | 0.842        |
| an error code pasted from a log    | 0.417         | 0.917        |

Neither retriever can be dropped -- each is decisively better on one family --
and equal-weight fusion scored *below the better single retriever on both*
(0.947 and 0.625). The reason is structural rather than incidental: at K=60 a
document at rank 1 in one list scores 1/61, and so does a document at rank 1 in
the other. So the weaker retriever's confidently wrong top hit ties the
stronger retriever's correct top hit, and outranks its second.

The fix is a per-source multiplier chosen from a property of the *query*:

    score(d) = sum over retrievers r that returned d:  w_r / (k + rank_r(d))

with ``w`` set by asking whether the query contains a literal identifier --
``has_identifier`` in ``domain/query.py``. If it does, keyword search leads; if
it does not, semantic search leads. The weights are coarse (1.0 and 0.5) and
deliberately not fitted: a value tuned to three decimal places on nineteen
planted queries would be overfitting dressed as rigour. What earns its place
here is the *mechanism* -- routing on an observable feature of the query --
not the constants.

Down-weighted, never removed. A misjudged query still receives both
retrievers' candidates, so the cost of the heuristic being wrong is bounded.
"""

from __future__ import annotations

import uuid
from collections.abc import Mapping

from app.modules.intelligence.query.parsing import has_identifier
from app.modules.intelligence.schemas.types import (
    FusedCandidate,
    RankedList,
    RetrievalSource,
)

#: The paper's default. Kept as a module constant so the settings value and
#: this share one meaning.
DEFAULT_K = 60


def reciprocal_rank_fusion(
    rankings: list[RankedList],
    *,
    k: int = DEFAULT_K,
    limit: int | None = None,
    pinned: tuple[uuid.UUID, ...] = (),
    weights: Mapping[RetrievalSource, float] | None = None,
) -> list[FusedCandidate]:
    """Fuse several ranked lists into one.

    Args:
        rankings: One entry per retriever, each already in rank order. Empty
            lists are allowed and contribute nothing — that is what makes the
            ablation table possible without a second code path.
        k: The rank-flattening constant. See the module docstring.
        weights: Per-source multiplier, defaulting to 1.0. See "When equal
            weights are wrong" in the module docstring — these are set from a
            property of the query, not tuned per corpus.
        limit: Truncate the fused result. Applied *after* fusion, so a
            document ranked 40th by both retrievers can still finish above one
            ranked 3rd by a single retriever.
        pinned: Ticket ids that must appear first regardless of score —
            explicit ``OS-1234`` references. A stated reference is a fact, not
            a similarity estimate, so it does not compete on score.

    Returns:
        Candidates in descending score. Ties break on ticket id so the order is
        deterministic; without that, two runs over the same data can return
        different orderings and every downstream cache and eval number becomes
        untrustworthy.
    """
    if k < 1:
        raise ValueError("k must be >= 1; k=0 would make rank 1 divide by zero")

    scores: dict[uuid.UUID, float] = {}
    ranks: dict[uuid.UUID, dict[RetrievalSource, int]] = {}

    for ranked in rankings:
        weight = 1.0 if weights is None else weights.get(ranked.source, 1.0)
        for position, ticket_id in enumerate(ranked.ticket_ids, start=1):
            scores[ticket_id] = scores.get(ticket_id, 0.0) + weight / (k + position)
            ranks.setdefault(ticket_id, {})[ranked.source] = position

    pinned_set = set(pinned)
    for ticket_id in pinned:
        ranks.setdefault(ticket_id, {})[RetrievalSource.REFERENCE] = 1

    fused = [
        FusedCandidate(ticket_id=ticket_id, score=score, ranks=dict(ranks[ticket_id]))
        for ticket_id, score in scores.items()
        if ticket_id not in pinned_set
    ]
    fused.sort(key=lambda c: (-c.score, c.ticket_id.bytes))

    head = [
        FusedCandidate(
            # An explicit reference is not a similarity estimate, so it is not
            # given a comparable score. 1.0 reads as "certain" to a caller
            # that only sorts, and the REFERENCE source says why.
            ticket_id=ticket_id,
            score=1.0,
            ranks=dict(ranks[ticket_id]),
        )
        for ticket_id in pinned
    ]

    out = head + fused
    return out[:limit] if limit is not None else out


def cascade(
    rankings: list[RankedList],
    *,
    leader: RetrievalSource,
    k: int = DEFAULT_K,
    limit: int | None = None,
    pinned: tuple[uuid.UUID, ...] = (),
) -> list[FusedCandidate]:
    """The leading retriever's order, with the other's finds appended below it.

    ## Why this ships instead of RRF

    RRF was implemented first, measured, and lost. The reason is worth stating
    precisely, because it is a property of the algorithm rather than a
    misconfiguration.

    RRF's value is its **agreement bonus**: a document both retrievers ranked
    reasonably beats a document one retriever loved. That bonus is also its
    failure mode when the retrievers are of unequal quality for the query at
    hand — and on this corpus they always are, in whichever direction the query
    happens to point. A document semantic search ranked 35th and keyword search
    ranked 2nd outscores the document semantic search ranked *1st*, so a noisy
    retriever's confident junk displaces the good retriever's best answer.

    Weighting the secondary retriever down does not fix it. Require that an
    agreement bonus can never lift a document above the leader's own top hit::

        1/(k + 2) + w/(k + 1)  <=  1/(k + 1)
        w  <=  1/(k + 2)   =  0.016  at k = 60

    At any weight small enough to be safe, the secondary contributes nothing
    but the ordering of documents *below* the leader's list. Which is exactly
    this function. So "keep RRF's agreement bonus" and "never rank worse than
    the better retriever" are not simultaneously satisfiable, and the choice
    has to be made deliberately rather than tuned around.

    The choice made here is the second, for one concrete reason: the next stage
    is an LLM reranker over this candidate pool. A reranker can reorder what it
    is given but cannot recover a document retrieval never returned, so the
    pool's job is **recall**, and precision at the head is the reranker's. A
    cascade maximises recall while guaranteeing the head is the leader's head.

    Guarantees, at any cutoff up to the leader's depth:

    * the first *n* results are exactly the leader's first *n*, so this is
      never worse than the leader alone;
    * documents only the secondary found are still present further down, so
      recall is at least the leader's and usually better;
    * ``agreed`` is still populated, so the reranker and the UI keep the
      cross-retriever agreement signal even though ordering no longer uses it.
    """
    by_source = {ranked.source: ranked for ranked in rankings}
    ranks: dict[uuid.UUID, dict[RetrievalSource, int]] = {}
    for ranked in rankings:
        for position, ticket_id in enumerate(ranked.ticket_ids, start=1):
            ranks.setdefault(ticket_id, {})[ranked.source] = position

    ordered: list[uuid.UUID] = []
    seen = set(pinned)
    # The leader first, in its own order, then everyone else in theirs. Sorted
    # by source name so the tail is deterministic when there are three or more.
    lead_first = [leader, *sorted(source for source in by_source if source != leader)]
    for source in lead_first:
        contribution = by_source.get(source)
        if contribution is None:
            continue
        for ticket_id in contribution.ticket_ids:
            if ticket_id not in seen:
                seen.add(ticket_id)
                ordered.append(ticket_id)

    for ticket_id in pinned:
        ranks.setdefault(ticket_id, {})[RetrievalSource.REFERENCE] = 1

    out = [
        FusedCandidate(ticket_id=ticket_id, score=1.0, ranks=dict(ranks[ticket_id]))
        for ticket_id in pinned
    ]
    out += [
        FusedCandidate(
            ticket_id=ticket_id,
            # A rank-derived confidence proxy, so score stays monotone with the
            # position actually returned. Comparable within one response only —
            # which is what the API documents.
            score=1.0 / (k + position),
            ranks=dict(ranks[ticket_id]),
        )
        for position, ticket_id in enumerate(ordered, start=1)
    ]
    return out[:limit] if limit is not None else out


def cascade_leader(rankings: list[RankedList]) -> RetrievalSource:
    """Which retriever's order the cascade follows.

    Semantic whenever it ran, keyword search otherwise. Not a per-query
    decision, and that is the point: an earlier version chose the leader from
    whether the query text contained an identifier, and it was measurably
    worse — the similar-issues query is a ticket's own text, error code
    included, so the heuristic fired on prose queries and handed ranking to the
    weaker retriever. Duplicate recall@20 fell from 1.000 to 0.842.

    Exact-token queries are not handled here at all. They are handled *before*
    ranking, by ``probe_identifiers``, which looks the token up with AND
    semantics and pins what it finds. Facts get looked up; only estimates get
    ranked. Once that separation is right, the leader does not need to be
    guessed.
    """
    sources = {ranked.source for ranked in rankings}
    return (
        RetrievalSource.SEMANTIC if RetrievalSource.SEMANTIC in sources else RetrievalSource.LEXICAL
    )


def leading_retriever(query_text: str) -> RetrievalSource:
    """Which retriever a query *looks* like it needs.

    Retained for the weighted-RRF row of the ablation, which is a
    measured-and-rejected configuration rather than the shipped one. See
    ``cascade_leader`` for why the shipped path does not use it.
    """
    return RetrievalSource.LEXICAL if has_identifier(query_text) else RetrievalSource.SEMANTIC


def retriever_weights(query_text: str, *, secondary: float = 0.5) -> dict[RetrievalSource, float]:
    """Choose which retriever leads, from the query text alone.

    A pure function of a string, so it is trivially testable and its decision
    is always explainable after the fact: the reason a result ranked where it
    did is "the query contained an identifier", not "the model felt strongly".

    ``secondary`` is the multiplier for whichever retriever is *not* leading.
    Never zero: down-weighting keeps the other retriever's candidates
    reachable, so a misjudged query degrades rather than fails.
    """
    lead = leading_retriever(query_text)
    other = RetrievalSource.SEMANTIC if lead is RetrievalSource.LEXICAL else RetrievalSource.LEXICAL
    return {lead: 1.0, other: secondary}


def rank_only(hits: list[tuple[uuid.UUID, float]], source: RetrievalSource) -> RankedList:
    """Turn scored hits into a ranked list, dropping the scores.

    The scores are dropped *here*, at the boundary, rather than being carried
    along and ignored later. If they were still in scope downstream, someone
    would eventually blend them.
    """
    seen: set[uuid.UUID] = set()
    ordered: list[uuid.UUID] = []
    for ticket_id, _score in sorted(hits, key=lambda pair: -pair[1]):
        if ticket_id not in seen:
            seen.add(ticket_id)
            ordered.append(ticket_id)
    return RankedList(source=source, ticket_ids=tuple(ordered))
