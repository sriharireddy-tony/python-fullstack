"""Framework-free primitives for retrieval.

Nothing here imports SQLAlchemy, LangChain, Chroma, or FastAPI. These types are
the vocabulary the node layer speaks, which is why the node layer can be tested
with plain dicts and no network.
"""

from __future__ import annotations

import uuid
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum


class Relation(StrEnum):
    """How one ticket relates to another, as judged by the reranker.

    Defined here rather than beside the table that stores it, so ``domain/``
    keeps its rule of importing nothing from SQLAlchemy. ``models.py`` imports
    it from here, which is the direction the dependency should point: the
    persistence layer knows about the domain vocabulary, not the reverse.
    """

    DUPLICATE = "duplicate"
    RELATED = "related"
    #: The same defect as one already closed -- so, a regression. Kept distinct
    #: from `duplicate` because the action differs: a duplicate gets linked, a
    #: recurrence gets someone asking why the fix did not hold.
    RECURRING = "recurring"
    UNRELATED = "unrelated"


class RetrievalSource(StrEnum):
    """Where a candidate came from.

    Carried all the way to the API response on purpose. When a result looks
    wrong, the first useful question is *which retriever found this* — a hit
    that only lexical search found and semantic search ranked nowhere is a
    different failure from the reverse.
    """

    SEMANTIC = "semantic"
    LEXICAL = "lexical"
    #: An explicit ``OS-1234`` reference in the query text. Not a guess, so it
    #: bypasses ranking entirely.
    REFERENCE = "reference"


class FusionMode(StrEnum):
    """How several rankings become one.

    Three named options rather than a boolean, because two of them were
    measured and rejected and that is worth keeping visible. See
    ``domain/fusion.py`` for the numbers.
    """

    #: Textbook Reciprocal Rank Fusion, every retriever equal.
    RRF = "rrf"
    #: RRF with a per-source multiplier chosen from the query.
    WEIGHTED_RRF = "weighted_rrf"
    #: The leading retriever's order, with the other's finds appended below.
    CASCADE = "cascade"


@dataclass(frozen=True, slots=True)
class RankedList:
    """One retriever's output, in rank order.

    Only the *order* is kept, not the scores. That is the whole reason
    Reciprocal Rank Fusion works without weight tuning: cosine similarity in
    the range 0.7-0.85 and a ``ts_rank_cd`` value of 0.09 are not comparable
    numbers, but "third" and "third" are.
    """

    source: RetrievalSource
    ticket_ids: tuple[uuid.UUID, ...]

    def __post_init__(self) -> None:
        if len(set(self.ticket_ids)) != len(self.ticket_ids):
            raise ValueError(f"{self.source} returned duplicate ids; ranks would be ambiguous")


@dataclass(frozen=True, slots=True)
class FusedCandidate:
    """A candidate after fusion, before hydration from Postgres."""

    ticket_id: uuid.UUID
    score: float
    #: Source → 1-based rank in that source's list. Absence means that
    #: retriever did not return this ticket at all.
    ranks: Mapping[RetrievalSource, int] = field(default_factory=dict)

    @property
    def sources(self) -> tuple[RetrievalSource, ...]:
        return tuple(self.ranks)

    @property
    def agreed(self) -> bool:
        """True when more than one retriever found it.

        A useful confidence signal on its own, and free — no model involved.
        """
        return len(self.ranks) > 1


@dataclass(frozen=True, slots=True)
class SimilarTicket:
    """A fused candidate hydrated with the fields a human needs to judge it.

    The text comes from Postgres, never from vector-store metadata. Chroma
    holds ids, vectors, and filter metadata only, so ticket content lives in
    exactly one place.
    """

    ticket_id: uuid.UUID
    reference: str
    title: str
    status: str
    priority: str
    team_id: uuid.UUID
    client_id: uuid.UUID
    created_at: datetime
    closed_at: datetime | None
    resolution_summary: str | None
    score: float
    ranks: Mapping[RetrievalSource, int]
    agreed: bool

    #: The reranker's verdict, when one ran. All three are None for a
    #: retrieval-only result, and that difference is deliberate: the UI shows a
    #: judged result differently from a ranked one, because "we think this is
    #: the same bug" is a much stronger claim than "this came up in a search".
    relation: Relation | None = None
    confidence: float | None = None
    reason: str | None = None
