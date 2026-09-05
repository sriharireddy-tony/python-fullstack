"""Keyword retrieval over tickets, via the generated ``search_vector`` column.

Separate from ``repository.py`` because it reads the *tickets* table rather
than the AI layer's own, and that is worth being visible rather than buried in
a file about AI bookkeeping.

The direction of the dependency is intentional and one-way: ``intelligence``
knows about tickets, ``tickets`` knows nothing about ``intelligence``.
"""

from __future__ import annotations

import uuid

from sqlalchemy import cast, func, select, text
from sqlalchemy.dialects.postgresql import ARRAY as PGARRAY
from sqlalchemy.dialects.postgresql import REAL, array
from sqlalchemy.ext.asyncio import AsyncSession

from app.modules.intelligence.ports import LexicalHit
from app.modules.tickets.models import Ticket

#: ``ts_rank_cd`` weights for the D, C, B, A labels, in that order — Postgres
#: reads the array backwards from the intuitive one. The generated column marks
#: title A, description B, steps C, so this makes a title match count ten times
#: a steps match. Must be ``real[]``; Postgres has no cast from
#: ``double precision[]`` and would reject the call.
_WEIGHTS = cast(array([0.1, 0.3, 0.6, 1.0]), PGARRAY(REAL))

#: Build an OR-query out of whatever ``to_tsvector`` makes of the input.
#:
#: This is the important line in the file. Two properties come from it:
#:
#: **One tokenizer.** The lexemes are produced by the same analyzer and the
#: same dictionary that built ``search_vector``, so query and document cannot
#: disagree about what a token is. Hand-tokenizing in Python got this wrong in
#: a way that silently broke every error-code lookup — see
#: ``prepare_lexical_text`` for the failure it caused.
#:
#: **OR, not AND.** ``plainto_tsquery`` and ``websearch_to_tsquery`` join terms
#: with ``&``, which is right for a search box and wrong here: the query is a
#: whole bug description, so requiring every term means a near-duplicate
#: written in different words matches nothing. ORing the lexemes and ranking by
#: ``ts_rank_cd`` gives what is actually wanted — rank by how much overlaps,
#: rather than filter on total overlap.
#:
#: Every lexeme goes through ``quote_literal``, so no fragment of user text is
#: ever parsed as a tsquery operator. The bind parameter is the injection
#: defence; this is what stops a stray ``!`` from inverting the query.
_OR_TSQUERY = """(
    SELECT string_agg(quote_literal(lexeme), ' | ')
    FROM unnest(tsvector_to_array(to_tsvector('english', :query_text))) AS lexeme
)::tsquery"""


class PostgresLexicalSearch:
    """``ts_rank_cd`` over the GIN-indexed ``search_vector``.

    Costs nothing extra: the column is a Postgres generated column that already
    exists for the ticket list's search box, and the index is already there.
    Half of a hybrid retriever for zero new infrastructure is a large part of
    why this design is worth having over vectors alone.

    The session must already be tenant-scoped — row-level security is what
    keeps this query inside one tenant, so there is no ``tenant_id`` filter
    here to get wrong.

    ## A known limit, stated rather than hidden

    Postgres' ``ts_rank_cd`` has no inverse-document-frequency term. It scores
    on term frequency, proximity, and the A/B/C/D weights, so a query word
    appearing in every ticket counts for as much as one appearing in a single
    ticket. That is why a rare identifier does not dominate the ranking the way
    BM25 would make it. Fixing it properly means a real BM25 implementation —
    OpenSearch, or an extension such as ``pg_search`` — which is a deliberate
    not-yet rather than an oversight, and the evaluation measures the cost of
    the choice instead of assuming it away.
    """

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def search(
        self,
        query_text: str,
        limit: int,
        exclude: set[uuid.UUID] | None = None,
    ) -> list[LexicalHit]:
        if not query_text.strip():
            # An empty query would match nothing useful; returning early keeps
            # the "lexical contributed nothing" case honest instead of sending
            # Postgres a query that errors.
            return []

        query = text(_OR_TSQUERY).bindparams(query_text=query_text)
        rank = func.ts_rank_cd(_WEIGHTS, Ticket.search_vector, query)

        stmt = (
            select(Ticket.id, rank.label("rank"))
            .where(
                Ticket.deleted_at.is_(None),
                Ticket.search_vector.op("@@")(query),
            )
            .order_by(rank.desc(), Ticket.id)
            .limit(limit + len(exclude or ()))
        )

        rows = await self._session.execute(stmt)
        excluded = exclude or set()
        hits = [
            LexicalHit(ticket_id=ticket_id, score=float(score))
            for ticket_id, score in rows.all()
            if ticket_id not in excluded
        ]
        return hits[:limit]

    async def search_exact(self, term: str, limit: int) -> list[LexicalHit]:
        """Tickets containing every token of ``term``.

        ``plainto_tsquery`` rather than the OR-query above, because it joins
        lexemes with ``&``. For ``ERR-40001`` that produces ``'err' & '-40001'``
        — exactly the AND needed. Matching on ``err`` alone would return every
        error report in the corpus, which is not a weaker answer but a wrong
        one.

        It also handles quoting itself, so the raw identifier can be passed
        through as a bind parameter with no escaping of our own.
        """
        if not term.strip():
            return []

        query = func.plainto_tsquery("english", term)
        rank = func.ts_rank_cd(_WEIGHTS, Ticket.search_vector, query)
        stmt = (
            select(Ticket.id, rank.label("rank"))
            .where(
                Ticket.deleted_at.is_(None),
                Ticket.search_vector.op("@@")(query),
            )
            .order_by(rank.desc(), Ticket.id)
            .limit(limit)
        )
        rows = await self._session.execute(stmt)
        return [
            LexicalHit(ticket_id=ticket_id, score=float(score)) for ticket_id, score in rows.all()
        ]

    async def by_numbers(self, numbers: tuple[int, ...]) -> dict[int, uuid.UUID]:
        """Resolve ``OS-1234`` references to ids.

        Silently drops numbers that do not exist or belong to another tenant —
        a stale reference in a description is common and is not an error worth
        failing a request over.
        """
        if not numbers:
            return {}
        rows = await self._session.execute(
            select(Ticket.ticket_number, Ticket.id).where(
                Ticket.ticket_number.in_(numbers),
                Ticket.deleted_at.is_(None),
            )
        )
        return dict(rows.all())  # type: ignore[arg-type]
