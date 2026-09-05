"""Output guardrails.

The input side reduces what reaches the model. This side decides what is
allowed to reach a human, and it assumes the model may be wrong or may have
been successfully manipulated.

Three checks, in increasing order of how much they matter:

1. **Schema** — handled upstream by constrained decoding plus Pydantic
   validation, so by the time a judgement arrives here its shape is already
   guaranteed. Nothing to do; noted so the absence is not mistaken for an
   oversight.
2. **Confidence floor** — a low-confidence judgement is dropped rather than
   shown with a small number next to it. People do not read the number.
3. **Citation grounding** — the one that carries real weight. Every judgement
   must name a ticket we supplied. This is what stops a model, or a ticket
   description that talked to it, from introducing a reference that was never
   retrieved.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from app.core.logging import get_logger
from app.modules.intelligence.domain.reports import RelationJudgement
from app.modules.intelligence.guardrails.rules import claims_authority

logger = get_logger(__name__)


@dataclass(slots=True)
class GuardedOutput:
    """What survived, and what did not."""

    judgements: list[RelationJudgement] = field(default_factory=list)
    #: Judgements naming a ticket outside the candidate set.
    ungrounded: int = 0
    #: Judgements dropped for low confidence.
    below_floor: int = 0
    #: Reasons rewritten because they claimed the system had acted.
    authority_claims: int = 0


def guard_output(
    judgements: list[RelationJudgement],
    *,
    allowed_references: set[str],
    confidence_floor: float,
    feature: str,
) -> GuardedOutput:
    """Filter model judgements down to what is safe and useful to show.

    Ordering note: grounding is checked **before** confidence. A judgement
    citing a ticket we never retrieved is not a weak answer to be filtered by
    confidence — it is evidence something went wrong, and it should be counted
    as that rather than absorbed into the "low confidence" bucket.
    """
    result = GuardedOutput()

    for judgement in judgements:
        if judgement.reference not in allowed_references:
            result.ungrounded += 1
            continue

        if judgement.confidence < confidence_floor:
            result.below_floor += 1
            continue

        claims = claims_authority(judgement.reason)
        if claims:
            # The model has described taking an action it cannot take. It has
            # no write tools, so nothing happened -- but a reader seeing "I
            # have closed this" believes it. Replaced rather than dropped,
            # because the classification itself may still be correct.
            result.authority_claims += 1
            logger.warning(
                "model output claimed an action it cannot perform",
                extra={"feature": feature, "claims": claims[:3]},
            )
            judgement = judgement.model_copy(update={"reason": "Looks like the same problem area."})

        result.judgements.append(judgement)

    if result.ungrounded:
        logger.warning(
            "model cited tickets outside the candidate set",
            extra={"feature": feature, "count": result.ungrounded},
        )

    return result
