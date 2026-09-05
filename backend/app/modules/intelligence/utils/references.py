"""Finding ticket references in free text.

`OS-1042` is the one identifier this system treats as load-bearing: it is how a
human names a ticket, and how a model cites one. Two separate pipelines needed
to extract it and each defined its own regex — `_REFERENCE_IN_PROSE` in the
analysis graph and `_REFERENCE` in the chat graph.

Two copies of a pattern that must agree is a defect waiting to happen, because
this pattern is a **security boundary**. Citation grounding works by extracting
the references a model claimed and checking each against what it actually
retrieved; a pattern that matches slightly less than the one used elsewhere
lets an ungrounded citation through unchallenged.

So there is one definition, and both graphs use it.
"""

from __future__ import annotations

import re

#: A ticket reference as written by a human or a model.
#:
#: Bounded at six digits rather than open-ended: unbounded ``\\d+`` would match
#: the digits of an arbitrarily long number and turn a stray "OS-" followed by
#: an id into a reference. Word boundaries on both sides so ``NOS-12`` and
#: ``OS-12X`` do not match.
REFERENCE_PATTERN = re.compile(r"\bOS-\d{1,6}\b")


def references_in(text: str | None) -> list[str]:
    """Every ticket reference in the text, in order, with duplicates kept.

    Order and duplicates are preserved deliberately. The caller validating
    citations wants to know *how many times* a model cited something it never
    retrieved, and deduplicating here would hide a model that repeated an
    invention five times behind a count of one.
    """
    return REFERENCE_PATTERN.findall(text or "")


def unique_references(text: str | None) -> list[str]:
    """Every distinct reference, first-seen order preserved.

    First-seen order rather than sorted, because the result is often shown to a
    person and the order a model mentioned things in carries meaning that
    alphabetical order destroys.
    """
    seen: dict[str, None] = {}
    for reference in references_in(text):
        seen.setdefault(reference, None)
    return list(seen)
