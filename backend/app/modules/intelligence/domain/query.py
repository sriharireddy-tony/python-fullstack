"""Turning a ticket into a retrieval query.

Two jobs, both pure: build the text a retriever should search with, and pull
out explicit ticket references so they can be answered as fact instead of
guessed at.
"""

from __future__ import annotations

import re

#: ``OS-1234`` as written by a human: optional space or hash, case-insensitive.
#: Bounded to six digits so a long number in prose ("error 30000000") is not
#: read as a reference.
_REFERENCE = re.compile(r"\bOS[-\s#]?(\d{1,6})\b", re.IGNORECASE)

#: Tokens as a human writes them, identifier shapes included: ``ERR-40001``,
#: ``EMP-1234``, ``DEV_SYNC_408``, ``api/v3``. Kept whole rather than split,
#: because splitting them is precisely the bug this replaced — see
#: ``prepare_lexical_text``.
_WORD = re.compile(r"[A-Za-z0-9_][A-Za-z0-9_./-]*[A-Za-z0-9_]|[A-Za-z0-9_]{2,}")

#: Words that carry no signal in a bug tracker where every ticket is a bug
#: reported by a customer. Removed only from the *lexical* query: the embedding
#: model handles them fine, and stripping them there would damage the sentence
#: structure it depends on.
_LEXICAL_STOPWORDS = frozenset(
    {
        "the",
        "and",
        "for",
        "not",
        "but",
        "with",
        "this",
        "that",
        "from",
        "are",
        "was",
        "were",
        "has",
        "have",
        "had",
        "when",
        "then",
        "there",
        "which",
        "while",
        "into",
        "your",
        "you",
        "our",
        "its",
        "it",
        "is",
        "in",
        "on",
        "of",
        "to",
        "at",
        "as",
        "by",
        "or",
        "an",
        "a",
        # Tracker-specific noise: true of nearly every row, so worthless as a
        # discriminator and actively harmful as an OR term.
        "issue",
        "bug",
        "ticket",
        "problem",
        "error",
        "customer",
        "client",
        "please",
        "kindly",
        "team",
        "getting",
        "showing",
        "happening",
    }
)

#: Cap on OR terms in the lexical query. Past roughly this many, every row with
#: any common word matches and ``ts_rank_cd`` is ranking noise — while the
#: query plan degrades from an index scan to something much worse.
MAX_LEXICAL_TERMS = 24


#: Tokens that only exact matching can find: an error code, an employee code,
#: a job id, a long numeric run, an API path. Deliberately shape-based rather
#: than a list of known prefixes — a corpus grows new identifier formats, and a
#: hardcoded list of them silently stops working.
_IDENTIFIER = re.compile(
    r"""
    \b(?:
        [A-Za-z]{2,}[_-]\d{3,}          # ERR-40001, JOB_925
      | [A-Za-z]{2,}_[A-Za-z]{2,}_\d+   # DEV_SYNC_408
      | [A-Z]{2,}[_-][A-Z]{2,}          # DEV_SYNC, API-KEY
      | \d{5,}                          # a long numeric run
      | [0-9a-f]{12,}                   # a trace or correlation id
      | /api/v\d+/[\w/-]+               # an endpoint path
    )\b
    """,
    re.VERBOSE,
)


#: How many identifiers from one query are probed. A description can mention
#: several; probing all of them turns one cheap lookup into an unbounded fan of
#: queries for diminishing returns.
MAX_PROBED_IDENTIFIERS = 3


def extract_identifiers(text: str, *, limit: int = MAX_PROBED_IDENTIFIERS) -> tuple[str, ...]:
    """Literal identifiers named in the text, in order of appearance.

    ## Why these are probed exactly rather than fed to the ranker

    An identifier is not a similarity signal, it is a *fact*. If a query says
    ``ERR-40001`` and exactly one ticket contains ``ERR-40001``, that ticket is
    the answer — no ranking required, and no ranking can do better. This is the
    same reasoning that makes an explicit ``OS-142`` reference pinned rather
    than ranked; an error code is the same kind of claim with a different
    syntax.

    Treating it as a ranking feature instead is what the first design did, by
    deciding which retriever to trust based on whether the query contained an
    identifier. That fell over on the case it most needed to handle: the
    similar-issues query *is* a ticket's own text, which routinely contains an
    error code that is unique to that ticket and therefore a pure distractor
    for finding its duplicates. The signal was real; using it to pick a leader
    was not.
    """
    found: list[str] = []
    for match in _IDENTIFIER.finditer(text):
        token = match.group(0)
        if token.lower() not in {existing.lower() for existing in found}:
            found.append(token)
        if len(found) >= limit:
            break
    return tuple(found)


def has_identifier(text: str) -> bool:
    """Whether the text names something only exact matching can find.

    The measurement that made this necessary: on paraphrased duplicates,
    semantic search beat keyword search outright (recall@20 1.000 against
    0.842); on an error code pasted from a log, keyword search beat semantic
    search just as decisively (0.917 against 0.417). Embeddings turn
    ``ERR-40001`` into an unremarkable point near "error" and cannot tell it
    from any other code, so neither retriever can be dropped.

    Used by the weighted-RRF configuration, which the ablation keeps as a
    rejected option. The shipped path uses ``extract_identifiers`` and probes
    the tokens exactly instead — see that function for why a fact should not be
    spent as a ranking hint.
    """
    return bool(_IDENTIFIER.search(text))


def extract_references(*texts: str | None) -> tuple[int, ...]:
    """Ticket numbers explicitly named in the text.

    "Same as OS-142" is a stated fact about a relationship. Treating it as one
    more thing for similarity search to hopefully rediscover would be strictly
    worse than reading it.

    Order is preserved and duplicates removed, so the first mention wins.
    """
    found: list[int] = []
    for text in texts:
        if not text:
            continue
        for match in _REFERENCE.finditer(text):
            number = int(match.group(1))
            if number not in found:
                found.append(number)
    return tuple(found)


def build_query_text(title: str | None, description: str | None, limit: int = 2000) -> str:
    """The text both retrievers search with.

    Title first and title-weighted the same way the embedding text is built, so
    the semantic query and the stored documents were produced by the same
    convention. A query built differently from the documents is the single most
    common cause of a hybrid system quietly under-performing.
    """
    parts = [(title or "").strip(), (description or "").strip()]
    return "\n\n".join(part for part in parts if part)[:limit]


def prepare_lexical_text(text: str, *, max_terms: int = MAX_LEXICAL_TERMS) -> str:
    """Reduce a query to the words worth searching for, and nothing else.

    ## Why this no longer builds a tsquery string

    It used to. It stripped every character Postgres treats as a tsquery
    operator — hyphens included — and joined the surviving words with ``|``.
    That produced a bug worth remembering: Postgres tokenizes ``ERR-40001``
    into the two lexemes ``'err'`` and ``'-40001'``, keeping the hyphen with
    the number. The query, having had its hyphen stripped, searched for
    ``40001``, which appears in no document. So error-code lookup — the single
    case keyword search exists to handle — matched nothing, silently, while
    every metric on paraphrase queries looked fine.

    The fix is not a better regex. It is to stop tokenizing in Python at all:
    the SQL layer now hands the text to ``to_tsvector`` and ORs the lexemes it
    produces, so the query is analysed by **exactly** the analyzer that built
    the index. Query and document cannot disagree about what a token is,
    because there is only one tokenizer.

    What is left for this function is the part Postgres cannot do: dropping
    words that are noise *in this domain* but not in English, and bounding the
    term count. Postgres' English dictionary removes "the" and "is"; it has no
    idea that in a bug tracker every ticket says "issue" and "customer", so
    those terms match everything and contribute nothing but rank noise.

    Returns an empty string when nothing usable survives, which callers treat
    as "skip lexical retrieval" rather than sending a query that matches
    everything.
    """
    terms: list[str] = []
    for match in _WORD.finditer(text.lower()):
        word = match.group(0)
        if word in _LEXICAL_STOPWORDS or word in terms:
            continue
        terms.append(word)
        if len(terms) >= max_terms:
            break
    return " ".join(terms)
