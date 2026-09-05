"""Guardrail catalogues: data, not logic.

Kept as data in one file because these lists change for reasons that have
nothing to do with code — a new PII format appears in tickets, a new injection
phrasing shows up in the wild. Editing a list is a smaller, safer, more
reviewable change than editing a function, and it means the *rules* can be
audited by someone who does not read Python.
"""

from __future__ import annotations

import re

#: PII patterns redacted before any text leaves the process for a hosted model.
#:
#: Applied on the *outbound* path specifically. The ticket itself keeps the
#: original text — support agents need the phone number to call the customer
#: back. What is not acceptable is that number reaching a third-party model and
#: possibly its logs. The redaction is a boundary control, not a data policy.
#:
#: Indian formats are included because that is who Keka's customers are; a
#: generic list would miss Aadhaar and PAN entirely, which are exactly the
#: identifiers a payroll ticket is most likely to contain.
PII_PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    ("[EMAIL]", re.compile(r"\b[\w.+-]+@[\w-]+\.[\w.-]{2,}\b")),
    # Indian PAN: five letters, four digits, one letter.
    ("[PAN]", re.compile(r"\b[A-Z]{5}\d{4}[A-Z]\b")),
    # Aadhaar: twelve digits, usually spaced in groups of four.
    ("[AADHAAR]", re.compile(r"\b\d{4}\s?\d{4}\s?\d{4}\b")),
    # Bank account: 9-18 digits. Deliberately after Aadhaar so a 12-digit
    # number is labelled as the more specific thing.
    ("[ACCOUNT]", re.compile(r"\b\d{9,18}\b")),
    ("[PHONE]", re.compile(r"(?<!\d)(?:\+91[-\s]?)?[6-9]\d{9}(?!\d)")),
    # Anything that looks like a credential. Two patterns, because one was not
    # enough and the red-team suite proved it.
    #
    # The original was
    #   \b(?:bearer|token|api[_-]?key|password)\b\s*[:=]?\s*\S{6,}
    # and against a realistic string --
    #   "Here is the token: Bearer sk-live-abc123def456ghi"
    # -- it matched "token: Bearer", because "Bearer" is itself six non-space
    # characters, and left the actual secret in the text. A redaction that
    # removes the label and keeps the value is worse than none: it looks like
    # it worked.
    #
    # So: allow a scheme word between the label and the value, and match the
    # value by shape rather than by "six of anything".
    (
        "[SECRET]",
        re.compile(
            r"(?i)\b(?:bearer|token|api[_-]?key|apikey|password|secret|credential)\b"
            r"\s*[:=]?\s*(?:bearer|basic)?\s*[A-Za-z0-9_\-./+=]{8,}"
        ),
    ),
    # Recognisable secret shapes with no label at all. People paste these on a
    # line by themselves, and a pattern that needs the word "token" nearby
    # misses every one of them.
    (
        "[SECRET]",
        re.compile(
            r"\b(?:sk|pk|rk)[-_](?:live|test|prod)?[-_]?[A-Za-z0-9]{8,}"
            r"|\bgh[pousr]_[A-Za-z0-9]{16,}"
            r"|\bxox[baprs]-[A-Za-z0-9-]{10,}"
            r"|\beyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{5,}"
        ),
    ),
]

#: Phrases that indicate an attempt to redirect the model rather than describe
#: a bug.
#:
#: **These are a signal, not a defence.** The real defence is structural: the
#: model has no write tools, its output is schema-constrained, and its citations
#: are validated. A phrase list is trivially evaded by rewording, so treating it
#: as the protection would be the actual vulnerability. What it is good for is
#: *detection* — a ticket matching several of these is worth logging, because it
#: means someone is probing.
INJECTION_MARKERS: tuple[str, ...] = (
    "ignore previous",
    "ignore the above",
    "ignore all previous",
    "disregard previous",
    "disregard the above",
    "new instructions",
    "system prompt",
    "you are now",
    "act as",
    "pretend to be",
    "reveal your",
    "print your instructions",
    "repeat the text above",
    "developer mode",
    "jailbreak",
    "sudo mode",
)

#: Words that would have the model claim an authority it does not have.
#: Checked on the *output* side: a suggestion that reads like a decision gets a
#: human to stop thinking, which is the specific harm this feature must avoid.
AUTHORITY_CLAIMS: tuple[str, ...] = (
    "i have closed",
    "i have assigned",
    "i have updated",
    "i have linked",
    "i have deleted",
    "this ticket is now",
    "i've closed",
    "i've assigned",
    "marked as duplicate",
    "has been closed",
)


def looks_like_injection(text: str) -> tuple[bool, list[str]]:
    """Which injection markers appear. Returns the markers for logging.

    Returning *what* matched rather than a bare boolean, because the log line
    is the whole point: "input flagged" tells an operator nothing, "input
    contained 'ignore previous' and 'system prompt'" tells them someone is
    testing the system.
    """
    lowered = text.lower()
    found = [marker for marker in INJECTION_MARKERS if marker in lowered]
    return bool(found), found


def claims_authority(text: str) -> list[str]:
    """Phrases where the model asserts it took an action."""
    lowered = text.lower()
    return [claim for claim in AUTHORITY_CLAIMS if claim in lowered]
