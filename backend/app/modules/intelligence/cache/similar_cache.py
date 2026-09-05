"""Serialising a similarity result into, and out of, the cache.

## Why this is its own module

It was two private methods on `SimilarityService`, which is where they were
written and not where they belong. Nothing in either one touches the service:
no session, no tenant, no dependencies — they are pure functions from a result
to a dict and back. Sitting on the service they looked like behaviour of the
search; they are behaviour of the *cache format*.

Separating them makes the round-trip testable on its own, and makes the one
rule that matters visible in a single file: **what is written must be readable
back**. A field added to the response and forgotten here degrades silently —
the fresh path shows it, the cached path returns null, and the difference only
appears the second time someone opens the same ticket. That has already
happened once, to the similarity score.

## What is deliberately not cached

Ticket text. The payload holds references, titles, and scores; the description
and resolution notes stay in Postgres. Caching them would put the same
sensitive content in a second store, which is the property the rest of this
design works to avoid — one store to secure, back up, and satisfy a deletion
request against.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from app.modules.intelligence.schemas.types import (
    Relation,
    RetrievalSource,
    SimilarityResult,
    SimilarTicket,
)


def to_cache(result: SimilarityResult) -> dict[str, Any]:
    """Serialise a result for Redis.

    Stores what the response needs and nothing more. Notably absent: the
    candidate objects and the hydrated catalogue. Caching ticket *text* in
    Redis would put the same sensitive content in a second store, which is
    the property the rest of this design works to avoid -- so the cache
    holds only what the panel displays.
    """
    return {
        "results": [
            {
                "ticket_id": str(item.ticket_id),
                "reference": item.reference,
                "title": item.title,
                "status": item.status,
                "priority": item.priority,
                "team_id": str(item.team_id),
                "client_id": str(item.client_id),
                "created_at": item.created_at.isoformat(),
                "closed_at": item.closed_at.isoformat() if item.closed_at else None,
                "resolution_summary": item.resolution_summary,
                "score": item.score,
                "similarity": item.similarity,
                "sources": [source.value for source in item.ranks],
                "agreed": item.agreed,
                "relation": item.relation.value if item.relation else None,
                "confidence": item.confidence,
                "reason": item.reason,
            }
            for item in result.results
        ],
        "total_candidates": result.total_candidates,
        "references": list(result.references),
        "trace": result.trace,
        "degraded": result.degraded,
        "grade": result.grade,
        "rewrites": result.rewrites,
        "rerank_model": result.rerank_model,
    }


def from_cache(payload: dict[str, Any], limit: int) -> SimilarityResult:
    """Rebuild a result from the cache.

    The per-retriever ranks are *not* restored faithfully -- only which
    retrievers found each result, in order. The exact rank numbers are
    diagnostics for one run and would be misleading if replayed as if they
    were this run's; what the UI actually uses is the source list and the
    agreement flag, and both survive.
    """
    results = [
        SimilarTicket(
            ticket_id=uuid.UUID(item["ticket_id"]),
            reference=item["reference"],
            title=item["title"],
            status=item["status"],
            priority=item["priority"],
            team_id=uuid.UUID(item["team_id"]),
            client_id=uuid.UUID(item["client_id"]),
            created_at=datetime.fromisoformat(item["created_at"]),
            closed_at=(datetime.fromisoformat(item["closed_at"]) if item["closed_at"] else None),
            resolution_summary=item["resolution_summary"],
            score=item["score"],
            ranks={
                RetrievalSource(source): index + 1 for index, source in enumerate(item["sources"])
            },
            agreed=item["agreed"],
            # `.get`, not `[...]`: entries written before this field existed
            # are still in the cache and must not raise on read.
            similarity=item.get("similarity"),
            relation=Relation(item["relation"]) if item.get("relation") else None,
            confidence=item.get("confidence"),
            reason=item.get("reason"),
        )
        for item in payload["results"][:limit]
    ]
    return SimilarityResult(
        results=results,
        total_candidates=payload["total_candidates"],
        references=tuple(payload["references"]),
        trace=[*payload["trace"], "cache:hit"],
        degraded=payload["degraded"],
        grade=payload.get("grade", ""),
        rewrites=payload.get("rewrites", 0),
        rerank_model=payload.get("rerank_model"),
        from_cache=True,
    )
