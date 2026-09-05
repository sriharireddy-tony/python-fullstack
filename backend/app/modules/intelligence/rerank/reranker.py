"""Reranking: turning a ranked list into a judged one.

## What the LLM adds that retrieval cannot

Retrieval answers "which tickets are textually closest". A person triaging a
bug wants to know something else: **is this the same bug, or just nearby?** Those
diverge constantly. Two payroll rounding bugs in different modules are textually
almost identical and are not duplicates. A payslip bug reported once as "blank
PDF" and once as "no earnings rows" shares nothing lexically and is the same
defect.

That distinction needs reading comprehension, so it is the one place in this
pipeline where an LLM earns its cost — and it is the *only* LLM call on the
similar-issues path.

## Why this is safe with untrusted input

Every ticket description reaching this prompt was written by a customer. Three
structural properties, not instructions, are what make that survivable:

* **The output is a schema.** ``{reference, relation, confidence, reason}``.
  There is no field in which the model could emit an action, so a successful
  injection produces a wrong classification, not a wrong *deed*.
* **Citations are validated.** A judgement naming a ticket that was not in the
  candidate set is dropped, so the model cannot introduce a ticket it invented
  or was told to mention.
* **Nothing is written.** A suggestion is stored as a suggestion. Accepting one
  is a human action through a separate endpoint with its own permission check.
"""

from __future__ import annotations

import uuid
from typing import Any

from app.core.errors import AppError
from app.core.logging import get_logger
from app.modules.intelligence.generation.grounded_answer import guard_output
from app.modules.intelligence.llm.registry import ModelRole
from app.modules.intelligence.models import Relation
from app.modules.intelligence.pipelines.state import SimilarityState
from app.modules.intelligence.ports import ChatModel
from app.modules.intelligence.prompts import RERANK_SYSTEM
from app.modules.intelligence.retrieval.deps import RetrievalDeps
from app.modules.intelligence.schemas.reports import RerankResult
from app.modules.intelligence.schemas.types import FusedCandidate

logger = get_logger(__name__)

#: How many candidates go into one call.
#:
#: One call for many candidates rather than one call each is what makes this
#: fit a 500-request daily quota at all. The number itself was measured down
#: from 20: the cost here is dominated by **output** tokens -- one judgement
#: per candidate, each with a written reason -- so halving the candidates
#: roughly halves the latency. Judging ten to display five loses nothing
#: measurable, because retrieval already puts every planted duplicate inside
#: the top five (recall@5 = 1.000), so candidates eleven to twenty were never
#: going to be shown.
MAX_CANDIDATES = 10

#: Judgements at or below this are dropped rather than shown. The panel's value
#: is that its contents are worth reading; a 0.3-confidence "related" is noise
#: with a number attached.
DEFAULT_CONFIDENCE_FLOOR = 0.55

#: Relations never worth surfacing, whatever the confidence.
DISCARDED = frozenset({Relation.UNRELATED})

#: Defined in `prompts/`, so it can be versioned and diffed
#: independently of the control flow that uses it.
SYSTEM = RERANK_SYSTEM


async def rerank_candidates(state: SimilarityState, deps: RetrievalDeps) -> dict[str, object]:
    """Classify each candidate, and drop what is not worth showing.

    Optional by design. With no registry, or with reranking disabled, or when
    the model is over budget, the graph returns the fused order unchanged. That
    is correct degradation rather than a failure: fused results are what Phase
    B shipped, and they were measured to be good.
    """
    candidates = state.get("candidates", [])[:MAX_CANDIDATES]
    if not candidates or deps.registry is None or not deps.rerank_enabled:
        return {"trace": ["rerank:skipped"]}

    catalogue = state.get("catalogue", {})
    if not catalogue:
        # The hydrate node did not run, so there is no text to compare. Failing
        # loudly here would be right in a test; in production the fused order
        # is still a good answer.
        logger.warning("rerank skipped: no hydrated candidate text")
        return {"trace": ["rerank:no-text"]}

    prompt = _prompt(state, candidates, catalogue)

    async def run(model: ChatModel) -> RerankResult:
        result = await model.generate_structured(
            SYSTEM, [{"role": "user", "content": prompt}], RerankResult
        )
        return RerankResult.model_validate(result, from_attributes=True)

    try:
        outcome = await deps.registry.call(
            ModelRole.RERANK, feature="similar_issues.rerank", run=run, prompt_text=prompt
        )
    except AppError as exc:
        logger.warning("rerank unavailable", extra={"error": type(exc).__name__})
        return {"trace": [f"rerank:failed:{type(exc).__name__}"]}

    result: RerankResult = outcome.value

    # Filtering runs through the output guardrail rather than being repeated
    # here. Citation grounding and the confidence floor are safety properties,
    # and a safety property implemented twice is a safety property that will
    # eventually disagree with itself.
    allowed = {catalogue[c.ticket_id]["reference"] for c in candidates if c.ticket_id in catalogue}
    guarded = guard_output(
        result.judgements,
        allowed_references=allowed,
        confidence_floor=deps.confidence_floor,
        feature="similar_issues.rerank",
    )

    kept = [j for j in guarded.judgements if j.relation not in DISCARDED]

    # Highest confidence first, then by how strong the claim is, so a
    # near-certain duplicate cannot sit below a confident "related".
    kept.sort(key=lambda j: (-j.confidence, _severity(j.relation)))

    logger.info(
        "rerank complete",
        extra={
            "model": outcome.model,
            "judged": len(result.judgements),
            "kept": len(kept),
            "dropped_ungrounded": guarded.ungrounded,
            "dropped_low_confidence": guarded.below_floor,
            "rewritten_authority_claims": guarded.authority_claims,
            "latency_ms": outcome.latency_ms,
            "used_fallback": outcome.used_fallback,
        },
    )
    return {
        "judgements": kept,
        "rerank_model": outcome.model,
        "rerank_cost": outcome.estimated_cost,
        "ungrounded_citations": guarded.ungrounded,
        "trace": [f"rerank:{outcome.model}:kept={len(kept)}/{len(result.judgements)}"],
    }


def _severity(relation: Relation) -> int:
    order = {Relation.DUPLICATE: 0, Relation.RECURRING: 1, Relation.RELATED: 2}
    return order.get(relation, 3)


def _prompt(
    state: SimilarityState,
    candidates: list[FusedCandidate],
    catalogue: dict[uuid.UUID, dict[str, Any]],
) -> str:
    """Build the comparison prompt.

    The candidate block carries **status and closure date** as well as text,
    because "recurring" is not a judgement about wording — it is a judgement
    about a closed ticket coming back, and the model cannot make it without
    knowing the ticket was closed.
    """
    lines = [f"NEW REPORT:\n{state['query_text']}\n", "EARLIER REPORTS:"]
    for candidate in candidates:
        entry = catalogue.get(candidate.ticket_id)
        if entry is None:
            continue
        status = entry["status"]
        closed = f", closed {entry['closed_at'][:10]}" if entry.get("closed_at") else ""
        lines.append(
            f"\n[{entry['reference']}] (status: {status}{closed})\n"
            f"{entry['title']}\n{(entry.get('description') or '')[:600]}"
        )
    return "\n".join(lines)
