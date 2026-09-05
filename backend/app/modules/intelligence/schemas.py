"""Request and response shapes for the AI endpoints.

Deliberately explicit about *why* a result was returned. A similarity feature
that shows a list with no explanation gets ignored after the first wrong
result; one that says "both retrievers ranked this highly" gives the reader
something to calibrate against.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class SimilarTicketOut(BaseModel):
    """One similar ticket, with its provenance."""

    model_config = ConfigDict(from_attributes=True)

    ticket_id: uuid.UUID
    reference: str = Field(description="Human reference, e.g. OS-1042.")
    title: str
    status: str
    priority: str
    team_id: uuid.UUID
    client_id: uuid.UUID
    created_at: datetime
    closed_at: datetime | None = None
    resolution_summary: str | None = Field(
        default=None,
        description=(
            "First part of the resolution notes, when the ticket was closed. "
            "The single most useful field on a similar ticket: how it was fixed."
        ),
    )

    score: float = Field(description="Fused rank score. Comparable within one response only.")

    #: Present only when the reranker ran. The distinction matters to the
    #: reader: a judged result is a claim ("this is the same bug"), an unjudged
    #: one is a search hit, and showing them identically would overstate the
    #: second.
    suggestion_id: uuid.UUID | None = Field(
        default=None,
        description="Set when this result was judged and stored, so it can be decided on.",
    )
    relation: str | None = Field(
        default=None, description="duplicate, recurring, or related. Null if not reranked."
    )
    confidence: float | None = Field(default=None, ge=0.0, le=1.0)
    reason: str | None = Field(
        default=None,
        description="The model's one-sentence justification, grounded in both tickets' text.",
    )
    sources: list[str] = Field(
        description=(
            "Which retrievers found this: semantic, lexical, or reference. "
            "A hit both retrievers agreed on is stronger evidence than either alone."
        )
    )
    agreed: bool = Field(description="True when more than one retriever returned it.")


class SimilarTicketsResponse(BaseModel):
    """The similar-issues payload.

    Carries the diagnostics alongside the results because this endpoint will be
    wrong sometimes, and "wrong with no way to see why" is what makes a feature
    get switched off.
    """

    ticket_id: uuid.UUID
    results: list[SimilarTicketOut]
    #: Empty is a real answer, not a failure. Most tickets genuinely have no
    #: near-duplicate, and inventing three weak ones trains people to ignore
    #: the panel.
    total_candidates: int = Field(description="Candidates after fusion, before the display limit.")
    references: list[int] = Field(
        default_factory=list,
        description="Ticket numbers named explicitly in the text, e.g. from 'same as OS-142'.",
    )
    trace: list[str] = Field(
        default_factory=list,
        description="Which nodes ran and what each returned. Diagnostics, not product copy.",
    )
    degraded: bool = Field(
        default=False,
        description=(
            "True when a retriever was unavailable and results came from the other alone."
        ),
    )
    #: Empty unless an input guardrail refused the request. Distinct from an
    #: empty result list, which means "we looked and found nothing".
    blocked_reason: str = ""
    grade: str = Field(default="", description="How retrieval was graded: good, weak, or empty.")
    rewrites: int = Field(default=0, description="How many corrective rewrite cycles ran.")
    rerank_model: str | None = Field(
        default=None, description="Which model judged these, if any. Null means retrieval only."
    )
    from_cache: bool = False


class SuggestionDecision(BaseModel):
    """A human's verdict on one suggestion.

    Every decision is a labelled training pair, which is why the endpoint
    exists at all rather than the panel being read-only: the feature and its
    own evaluation dataset are the same thing.
    """

    accepted: bool = Field(description="True to agree with the suggestion, false to reject it.")


class SuggestionOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    ticket_id: uuid.UUID
    related_ticket_id: uuid.UUID | None
    relation: str | None
    confidence: float | None
    reason: str | None
    model: str | None
    status: str
    decided_at: datetime | None = None


class AnalysisStepOut(BaseModel):
    """One agent turn, as shown in the UI.

    The step log is the difference between a report a reader can weigh and one
    they must simply believe. "It searched for similar tickets and found four,
    then checked the history of two" is checkable; a paragraph of conclusions
    is not.
    """

    model_config = ConfigDict(from_attributes=True)

    turn: int
    tool_name: str | None
    tool_args: dict[str, Any] | None
    result_summary: str | None
    created_at: datetime


class AnalysisReportOut(BaseModel):
    """The agent's structured findings."""

    summary: str
    likely_area: str
    is_recurring: bool
    related_references: list[str]
    suggested_next_steps: list[str]
    confidence: float


class AnalysisRunOut(BaseModel):
    """One analysis run, at whatever stage it has reached."""

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    ticket_id: uuid.UUID
    status: str
    #: Null while queued or running. The client polls until it is not.
    report: AnalysisReportOut | None = None
    #: True when a limit stopped the run. The report, if any, is what the agent
    #: had gathered by then -- worth reading, and worth labelling.
    partial: bool = False
    #: Which limit fired, in plain words, or the failure.
    stop_reason: str | None = None
    model: str | None = None
    #: True when a weaker fallback model produced this. Surfaced because a
    #: reader calibrating on "the AI said" deserves to know which one.
    used_fallback_model: bool = False
    turns: int = 0
    tool_calls: int = 0
    estimated_cost: float = 0.0
    latency_ms: int | None = None
    helpful: bool | None = None
    steps: list[AnalysisStepOut] = Field(default_factory=list)
    #: Which graph nodes ran, for the diagnostics panel.
    trace: list[str] = Field(default_factory=list)


class AnalysisRating(BaseModel):
    """Thumbs up or down on a finished analysis."""

    helpful: bool = Field(description="True if the analysis was useful.")
