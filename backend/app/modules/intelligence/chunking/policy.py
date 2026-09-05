"""How a ticket becomes embeddable text — and why it is never split.

## There is no chunker here, and that is the decision

Chunking exists to solve two problems: fitting a document that exceeds the
model's context window, and giving retrieval a unit smaller than "the whole
document" so a match points at the relevant passage rather than at a 40-page
PDF.

Neither applies. A ticket is title plus description plus steps — roughly one to
two thousand characters, well inside any embedding model's window, and already
the exact unit a human wants back. Splitting one would mean a search could
return "the third paragraph of OS-1042", which is not a thing anybody wants to
read; the answer to "what is similar to this bug" is a bug, not a fragment.

So the policy is **one chunk per ticket**, and this module is where that is
stated rather than left implicit in whoever calls the embedder.

## What this module does instead: field-level truncation

The one real chunking concern that survives is *bounding*. An unbounded field
means the first N tokens silently decide the vector, with nothing recording
what was cut. So each field has its own cap, and the order matters — title
first, so when truncation happens the title is what survives.

The caps are configuration (`EMBED_TITLE_MAX` and friends) rather than
constants, because they are part of the embedding identity: `compute_source_hash`
folds in the model and instruction version, and changing a cap changes the text,
which changes the hash, which forces a re-embed. Nothing goes stale quietly.

## When this file would grow a real chunker

If tickets ever carry attachments whose text is indexed — a stack trace, a log
excerpt, a support-call transcript — those are documents rather than records
and would need splitting. That is the trigger to revisit, and the reason this
module exists as a named place rather than three slice expressions inside the
embedding service.
"""

from __future__ import annotations

from app.core.config import settings
from app.modules.tickets.models import Ticket

#: The separator between fields in the assembled text.
#:
#: A blank line rather than a space: it survives whitespace normalisation in
#: the hash, and it gives the embedding model a structural cue that title,
#: description and steps are distinct rather than one run-on sentence.
FIELD_SEPARATOR = "\n\n"


def build_embedding_text(ticket: Ticket) -> str:
    """The text that represents a ticket for similarity purposes.

    Bounded, because the model truncates at its token limit anyway — and an
    unbounded field means the first N tokens silently decide the vector with no
    indication of what was cut. Title first, so if truncation happens the title
    is what survives.

    Deliberately excluded:

    * **comments** — they change constantly, so including them means
      re-embedding forever and the vector never settles.
    * **resolution notes** — written *after* the bug is understood. Including
      them leaks the outcome into a signal used at triage time, which inflates
      offline scores and produces a system that performs worse in reality.
    * **client name** — would cluster tickets by customer rather than by
      problem. "Acme payroll bug" and "Acme leave bug" share "Acme". The client
      is a metadata filter, not part of the meaning.
    """
    parts = [
        (ticket.title or "")[: settings.EMBED_TITLE_MAX],
        (ticket.description or "")[: settings.EMBED_DESCRIPTION_MAX],
        (ticket.steps_to_reproduce or "")[: settings.EMBED_STEPS_MAX],
    ]
    return FIELD_SEPARATOR.join(part.strip() for part in parts if part and part.strip())


def chunk_count(ticket: Ticket) -> int:
    """How many chunks this ticket produces: one, or none if it has no text.

    Exists so callers can ask the question without knowing the answer is
    always one — the day attachments are indexed, this is the function that
    starts returning something else, and the callers do not change.
    """
    return 1 if build_embedding_text(ticket) else 0
