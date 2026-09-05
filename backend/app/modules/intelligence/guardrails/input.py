"""Input guardrails.

Everything here runs **before** any model sees the text, and everything here is
cheap and deterministic — no LLM judges an input. A guardrail that costs a model
call has doubled the attack surface it exists to reduce.

## The order matters

1. **Length** — truncate first, so every later regex runs over bounded input.
   A 500 KB pasted log would otherwise make the PII patterns quadratic.
2. **Scope** — is this even a bug report? Cheapest possible rejection.
3. **PII redaction** — before the text can leave the process.
4. **Injection detection** — logged, and it does not block. See below.

## Why injection detection does not block

Because a phrase list cannot be the defence. Rewording defeats it in seconds,
so treating a clean scan as safety would be the real vulnerability. The
structural properties are the defence: no write tools, schema-constrained
output, validated citations. Detection here earns its place as *telemetry* — a
rising count means someone is probing, which is worth knowing.

Blocking on it would also break the product. "Ignore the previous payslip run
and reprocess" is a real sentence a support agent writes.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from app.core.logging import get_logger
from app.modules.intelligence.guardrails.rules import PII_PATTERNS, looks_like_injection

logger = get_logger(__name__)

#: Hard cap on text sent to a model. A bug description past this is a pasted
#: log, and the first 8000 characters contain the report.
MAX_INPUT_CHARS = 8000

#: Below this there is nothing to search for. "it broke" retrieves noise, and
#: noise shown confidently is worse than an empty panel.
MIN_INPUT_CHARS = 12


@dataclass(slots=True)
class GuardedInput:
    """The result of guarding one piece of text."""

    text: str
    #: True when the request should not proceed at all.
    blocked: bool = False
    reason: str = ""
    #: What was redacted, by label. Counts only — never the values, or the log
    #: becomes the leak the redaction was preventing.
    redactions: dict[str, int] = field(default_factory=dict)
    #: Injection markers found. Recorded, not acted on.
    injection_markers: list[str] = field(default_factory=list)
    truncated: bool = False


def redact_pii(text: str) -> tuple[str, dict[str, int]]:
    """Replace anything that looks like personal data with a label.

    Labels rather than deletion, because the model still needs to know a phone
    number *was* there — two tickets that both say "[PHONE] not receiving OTP"
    are more obviously similar than two that say "not receiving OTP" with a gap.
    Structure is preserved; the value is not.
    """
    counts: dict[str, int] = {}
    for label, pattern in PII_PATTERNS:
        text, hits = pattern.subn(label, text)
        if hits:
            counts[label] = hits
    return text, counts


def guard_input(text: str, *, feature: str) -> GuardedInput:
    """Run every input guardrail, in order."""
    original_length = len(text)
    truncated = original_length > MAX_INPUT_CHARS
    working = text[:MAX_INPUT_CHARS]

    if len(working.strip()) < MIN_INPUT_CHARS:
        return GuardedInput(
            text=working,
            blocked=True,
            reason="Not enough text to search with.",
            truncated=truncated,
        )

    redacted, counts = redact_pii(working)
    flagged, markers = looks_like_injection(redacted)

    if flagged:
        # Logged at warning because it is worth an operator's attention, but
        # deliberately not blocking -- see the module docstring.
        logger.warning(
            "input contains injection markers",
            extra={"feature": feature, "markers": markers[:5], "count": len(markers)},
        )

    if counts:
        logger.info(
            "input redacted before leaving the process",
            extra={"feature": feature, "redactions": counts},
        )

    return GuardedInput(
        text=redacted,
        redactions=counts,
        injection_markers=markers,
        truncated=truncated,
    )
