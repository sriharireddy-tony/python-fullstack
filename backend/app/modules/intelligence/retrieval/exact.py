"""Exact-match nodes: the answers that need no ranking.

Both nodes here answer with facts rather than estimates, and both run before
anything expensive. A ticket reference and an error code are *claims about
identity*; ranking them alongside similarity guesses would be strictly worse
than reading them.

Everything they find goes into ``pinned``, which fusion places above the
ranked results and the score floor does not filter.
"""

from __future__ import annotations

import uuid

from app.core.logging import get_logger
from app.modules.intelligence.pipelines.state import SimilarityState
from app.modules.intelligence.query.parsing import extract_identifiers, extract_references
from app.modules.intelligence.retrieval.deps import RetrievalDeps

logger = get_logger(__name__)

#: A token is only treated as an identifier if it matches **exactly one**
#: ticket. Anything matching several is a shared token -- a module name, an
#: endpoint path, a common status code -- and its hits belong to the ranker.
#:
#: Set to 3 first, and measured: pinning a two-or-three-way match cost one
#: duplicate query its rank-1 result, because a shared ``/api/v3/...`` path in
#: a prose description pinned an unrelated ticket above the real duplicate.
#: Requiring uniqueness removed that regression and kept exact-code lookup
#: perfect, which is the whole point of the bound -- it is what separates "this
#: names a ticket" from "this word appears in some tickets".
MAX_PINS_PER_IDENTIFIER = 1


async def resolve_references(state: SimilarityState, deps: RetrievalDeps) -> dict[str, object]:
    """Find explicit ``OS-1234`` mentions and resolve them to ids.

    A CS agent who writes "same as OS-142" has told us the answer. Making
    similarity search rediscover it would be slower and less reliable than
    reading it.

    Unresolvable numbers are dropped rather than reported. A typo or a
    reference to a deleted ticket is ordinary, and failing the request over it
    would be worse than showing one fewer result.
    """
    numbers = extract_references(state["query_text"])
    if not numbers:
        return {"references": (), "pinned": (), "trace": ["references:none"]}

    resolved = await deps.lexical.by_numbers(numbers)
    own_id = state.get("ticket_id")
    pinned = tuple(
        ticket_id
        for number in numbers
        if (ticket_id := resolved.get(number)) is not None and ticket_id != own_id
    )
    return {
        "references": numbers,
        "pinned": pinned,
        "trace": [f"references:{len(pinned)}"],
    }


async def probe_identifiers(state: SimilarityState, deps: RetrievalDeps) -> dict[str, object]:
    """Look up literal identifiers exactly, and pin what they find.

    ## Why a probe rather than a ranking signal

    An earlier design used "does the query contain an identifier" to decide
    which retriever to trust more. Measured, it made things worse on the case
    that matters most: the similar-issues query is a ticket's *own* text, and
    every ticket in this corpus carries an error code unique to itself. So the
    signal fired on prose queries, handed the ranking to keyword search, and
    dropped duplicate recall from 1.000 to 0.842.

    The mistake was using a fact as a hint. ``ERR-40001`` does not suggest
    which retriever is better — it names a ticket. So it is looked up with AND
    semantics and pinned, and the general ranking is left alone.

    That also makes the ticket-as-query case correct for free: a ticket's own
    code appears only in itself, and the ticket itself is excluded, so the
    probe finds nothing and changes nothing.

    Two bounds keep it honest:

    * at most ``MAX_PROBED_IDENTIFIERS`` identifiers per query, so a
      log-dump description cannot fan out into many queries;
    * a token is pinned only when it matches exactly one ticket, so a token
      that is not actually identifying gets ranked rather than pinned.
    """
    already = tuple(state.get("pinned", ()))
    if not deps.exact_probe:
        return {"trace": ["identifiers:disabled"]}

    identifiers = extract_identifiers(state["query_text"])
    if not identifiers:
        return {"trace": ["identifiers:none"]}

    own_id = state.get("ticket_id")
    seen: set[uuid.UUID] = {*already}
    if own_id is not None:
        seen.add(own_id)

    pinned: list[uuid.UUID] = []
    for term in identifiers:
        # Over-fetch by one: a probe returning exactly the cap is
        # indistinguishable from one returning far more, and pinning in that
        # case is the failure this bound exists to prevent.
        hits = await deps.lexical.search_exact(term, limit=MAX_PINS_PER_IDENTIFIER + 1)
        candidates = [hit.ticket_id for hit in hits if hit.ticket_id not in seen]
        if not candidates or len(hits) > MAX_PINS_PER_IDENTIFIER:
            continue
        for ticket_id in candidates:
            seen.add(ticket_id)
            pinned.append(ticket_id)

    if pinned:
        logger.info(
            "identifier probe pinned exact matches",
            extra={"identifiers": len(identifiers), "pinned": len(pinned)},
        )
    return {
        "pinned": already + tuple(pinned),
        "trace": [f"identifiers:{len(identifiers)}:pinned={len(pinned)}"],
    }
