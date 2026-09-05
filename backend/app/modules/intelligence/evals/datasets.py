"""The golden set: which tickets are genuinely duplicates of which.

## Where the labels come from, and why that matters

These clusters are **planted by the demo seed**. Five real Keka support
scenarios, each written two to four ways as different clients would report
them, raised against different clients on different dates. Every ticket in a
cluster describes the same underlying defect.

That gives a labelled set with no annotation effort, which is the only reason
Tier-1 evaluation exists this early. It also has a specific weakness worth
stating rather than hiding: **the paraphrases were written by the same author
as the system being measured.** They are plausible rewordings, but they are not
what a real CS agent under time pressure types. So the absolute numbers here
are optimistic, and their honest use is *comparative* — does fusion beat
lexical alone, did this change make retrieval better or worse. A move from
0.71 to 0.83 recall on this set is real information; "recall is 0.83" as a
claim about production is not.

The real signal arrives in Phase G, when accept and reject on the suggestion
panel start filling ``ai_eval_pairs`` with labels produced by the people who
actually triage bugs. This set is the scaffolding that gets us there.

This module is the single source of truth for the clusters — ``scripts/seed_demo``
imports it rather than keeping its own copy, because two lists that must agree
eventually disagree.
"""

from __future__ import annotations

import re
import uuid
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.modules.tickets.models import Ticket

#: One variant of a planted duplicate: the title and description a *different*
#: client would have used for the same underlying defect.
Variant = tuple[str, str]

#: base scenario title → the same bug, as a different customer reported it.
#:
#: ## What these are for
#:
#: These twenty tickets are the *related* half of the corpus: 80 distinct bugs
#: in `scripts/seed_scenarios.py`, plus these, giving 100 tickets with known
#: ground truth. Every ticket in a cluster is the same underlying defect, so
#: "did retrieval find the duplicate" has a checkable answer.
#:
#: ## Two rules they follow, both learned the hard way
#:
#: **The description varies, not just the title.** An early version paraphrased
#: only titles and reused the base description verbatim. Every configuration
#: scored 1.000 on every metric, which reads as success and means the benchmark
#: has no power to distinguish anything: with identical descriptions the task is
#: exact-text matching, which both retrievers solve perfectly.
#:
#: **The corpus around them contains no verbatim repeats.** The same early
#: version drew 220 tickets from 64 scenarios by weighted sampling, so 200 of
#: them sat in an exact-duplicate group -- nine tickets sharing a title word for
#: word. Paraphrase recall measured against that is optimistic in a way the
#: number does not admit to. Each scenario is now used exactly once.
#:
#: ## Why the clusters stress different retrievers
#:
#: A golden set where every cluster favours the same retriever makes an ablation
#: look decisive while proving nothing. So:
#:
#: * **Payslip, leave carry-forward, half-day** — near-disjoint vocabulary.
#:   "Payslip PDF blank" against "salary statement opens with column headings
#:   only" shares almost no terms; semantic search should carry these.
#: * **TDS, PF challan** — hinge on rare domain tokens (`80C`, `ECR`, `EPFO`)
#:   that an embedding flattens into "something about tax"; keyword search
#:   should win.
#: * **SSO, biometric sync, webhook** — carry literal identifiers and error
#:   codes, the case where only exact matching works.
#: * **The rest** — in between, with some shared terms and some not.
GOLDEN_CLUSTERS: dict[str, tuple[Variant, ...]] = {
    # --- semantic-favouring: the wording barely overlaps
    "Payslip PDF generates blank for contractors": (
        (
            "Salary slip shows no data for contract staff",
            "The salary statement opens with the column headings and nothing "
            "underneath for anyone on a fixed-term agreement. Regular employees "
            "are fine on the same cycle. Finance is reading the figures off the "
            "screen instead of the document.",
        ),
        (
            "No earnings rows on the monthly statement for non-permanent employees",
            "The wage breakdown is completely missing for staff who are not on the "
            "permanent rolls -- the file downloads and opens, it just has no line "
            "items. Started around the time we converted a batch of people to "
            "temporary contracts.",
        ),
    ),
    "Leave balance short by one day after carry-forward": (
        (
            "Annual leave opening balance is one day less than last year's closing",
            "This year's starting entitlement does not match what people finished "
            "the previous year with. The gap is exactly one working day for "
            "everyone we have checked, which is about thirty people so far.",
        ),
    ),
    "Half-day leave deducts a full day from the balance": (
        (
            "Applying for a half day removes a whole day of casual leave",
            "An employee taking a half day sees their casual leave balance drop by "
            "one, not by half. The attendance record for that date correctly shows "
            "a half day worked, so only the balance figure disagrees.",
        ),
    ),
    "Manager change does not cascade to pending leave approvals": (
        (
            "Leave requests stay with the old reporting manager after a transfer",
            "When somebody moves team, the requests they had already raised remain "
            "in their previous manager's approval list. The new manager sees "
            "nothing to approve, and the previous one can still act on them even "
            "though the person no longer reports to them.",
        ),
    ),
    "Employee document upload fails silently over five megabytes": (
        (
            "Large scanned documents disappear after a successful upload message",
            "Uploading a scan of an identity document shows a green confirmation "
            "and then the file is not in the employee's document list. Smaller "
            "files of the same type work, so it looks size-related.",
        ),
    ),
    # --- lexical-favouring: rare domain tokens carry the meaning
    "TDS computed on gross instead of taxable income": (
        (
            "TDS too high, 80C declaration not considered",
            "Monthly TDS is far above what employees expect. The 80C investment "
            "proofs uploaded in April are not reflected in the computation, and "
            "neither is the HRA exemption.",
        ),
        (
            "Income tax deduction ignoring investment declarations",
            "Withholding is being calculated on the full salary figure. The savings "
            "declarations submitted at the start of the year are not being applied "
            "at all, so take-home is well below what the tax projection showed.",
        ),
    ),
    "PF challan total does not match the contribution report": (
        (
            "ECR file total differs from the PF contribution statement",
            "The ECR we upload to EPFO does not tie to the contribution report by "
            "the employer share for mid-month joiners. We have been editing the "
            "challan by hand each month before submitting.",
        ),
    ),
    "ESI deduction continues above the wage ceiling": (
        (
            "ESI still being deducted after an employee crossed the wage limit",
            "Two employees whose gross went past the ESI ceiling following an "
            "increment are still having the contribution taken. It should have "
            "stopped at the end of the contribution period.",
        ),
    ),
    # --- identifier-favouring: literal codes and device references
    "SSO login loops back to the sign-in page": (
        (
            "Cannot sign in with company SSO, redirects to login again",
            "Staff using the corporate identity provider are bounced back to the "
            "sign-in screen instead of reaching the dashboard. Password login "
            "still works. Browser console shows SAML_RESP_INVALID on the callback.",
        ),
        (
            "Single sign-on redirect loop after identity provider authentication",
            "Authentication completes at the provider and the browser then cycles "
            "between the callback URL and the entry page until the session times "
            "out. Affects everyone on the corporate domain.",
        ),
    ),
    "Biometric attendance sync stopped for one location": (
        (
            "Attendance punches not coming through from the branch office",
            "No clock-in records have arrived from the Hyderabad site since the "
            "weekend. Every other office is reporting normally and the device "
            "itself has the punches stored locally.",
        ),
        (
            "Device sync failure, no attendance records since Tuesday",
            "The time-clock hardware at one site stopped transmitting. Error code "
            "DEV_SYNC_408 appears in the integration log every fifteen minutes, "
            "and the device console shows the records queued.",
        ),
    ),
    "Webhook retries stop after the first failure": (
        (
            "Employee-created webhook delivered once and never retried",
            "Our endpoint returned a 502 during a deploy and the event was never "
            "resent. The delivery log shows a single attempt. New joiners are "
            "missing from our internal directory whenever we restart the service.",
        ),
    ),
    # --- in between
    "Comp-off expiry not honoured": (
        (
            "Compensatory off lapsing after thirty days instead of ninety",
            "Comp-offs earned for weekend work are disappearing a month later even "
            "though the policy screen shows a ninety-day validity. Employees are "
            "losing time they earned with no warning email.",
        ),
    ),
    "Weekly off marked as absent for night-shift staff": (
        (
            "Night shift employees showing absent on their rostered off day",
            "People on the shift that starts at ten at night are marked absent on "
            "their weekly off. The shift crosses midnight, so it looks like the off "
            "day is judged against the wrong calendar date.",
        ),
    ),
    "Bulk employee import skips rows with a duplicate email silently": (
        (
            "Import reports success but creates fewer employees than the file has",
            "We uploaded 300 rows and the summary said 300 imported. Only 287 "
            "employees exist afterwards. The missing ones share an email with "
            "somebody already in the system, and nothing in the report said so.",
        ),
    ),
    "Attendance export missing the last day of the month": (
        (
            "Monthly attendance download ends a day early",
            "The export for a thirty-one day month stops at the thirtieth. The "
            "attendance screen shows the final day correctly, so it is the export "
            "range rather than the data.",
        ),
    ),
    "Review cycle cannot close while one review is in draft": (
        (
            "Appraisal cycle blocked by a self-review from an employee who left",
            "The cycle will not close because one self-assessment is still in draft "
            "for somebody who exited in November. There is no option to withdraw or "
            "force-complete it, and 900 people are waiting on the cycle.",
        ),
    ),
}


@dataclass(frozen=True, slots=True)
class EvalQuery:
    """One evaluation query and its ground truth.

    ``exclude`` is separate from ``relevant`` on purpose, because the two query
    families need opposite behaviour. A duplicate-cluster query *is* a ticket,
    so that ticket must be excluded from retrieval or it is its own perfect top
    hit and every metric is inflated. An identifier query is free text with no
    ticket of its own, and excluding anything would remove the only correct
    answer.

    On the ceiling this implies: a cluster of four tickets yields four queries
    with three relevant documents each, so recall is measured against three,
    not against "everything a human might call related". Precision here is
    therefore pessimistic in a specific way — a genuinely related ticket
    outside the planted cluster counts as a miss.
    """

    label: str
    title: str
    description: str | None
    cluster: str
    relevant: frozenset[uuid.UUID]
    #: Removed from the candidate set before scoring. See above.
    exclude: uuid.UUID | None = None

    @property
    def ideal_hits(self) -> int:
        return len(self.relevant)


async def load_golden_queries(session: AsyncSession) -> list[EvalQuery]:
    """Resolve the clusters against the database.

    Titles rather than ids, because ids are generated per seed run. Matching on
    the exact title is safe here: these are scenario titles, and the seed writes
    them verbatim.

    A ticket whose title appears in more than one cluster would corrupt the
    labels, so that is checked rather than assumed.
    """
    title_to_cluster: dict[str, str] = {}
    for base, variants in GOLDEN_CLUSTERS.items():
        for title in (base, *(variant[0] for variant in variants)):
            if title in title_to_cluster and title_to_cluster[title] != base:
                raise ValueError(f"title {title!r} appears in two clusters; labels are ambiguous")
            title_to_cluster[title] = base

    rows = await session.execute(
        select(Ticket.id, Ticket.ticket_number, Ticket.title, Ticket.description).where(
            Ticket.title.in_(list(title_to_cluster)),
            Ticket.deleted_at.is_(None),
        )
    )
    found = list(rows.all())

    members: dict[str, list[tuple[uuid.UUID, int, str, str | None]]] = {}
    for ticket_id, number, title, description in found:
        members.setdefault(title_to_cluster[title], []).append(
            (ticket_id, number, title, description)
        )

    queries: list[EvalQuery] = []
    for cluster, entries in members.items():
        if len(entries) < 2:
            # A cluster with one member gives no relevant documents, so it
            # cannot contribute to recall. Skipped rather than counted as a
            # perfect or a failed query -- either would distort the average.
            continue
        ids = {entry[0] for entry in entries}
        for ticket_id, number, title, description in entries:
            queries.append(
                EvalQuery(
                    label=f"OS-{number}",
                    title=title,
                    description=description,
                    cluster=cluster,
                    relevant=frozenset(ids - {ticket_id}),
                    exclude=ticket_id,
                )
            )
    queries.sort(key=lambda q: q.label)
    return queries


#: An error code as the seed writes it. Unique per ticket by construction.
_ERROR_CODE = re.compile(r"\bERR-\d{5}\b")

#: How a developer actually arrives with a code: pasted from a log, with a
#: sentence of context and none of the ticket's own wording. That absence is
#: the point — the query shares no prose with the target, so semantic search
#: has almost nothing to work with and exact matching has everything.
_IDENTIFIER_PHRASINGS = (
    "Seeing {code} in the logs. Has this been reported before?",
    "{code} showing up again in production. Any existing ticket?",
    "Getting {code} from the API. Previous occurrence?",
)


async def load_identifier_queries(session: AsyncSession, limit: int = 24) -> list[EvalQuery]:
    """Queries that name a literal identifier, with the ticket that contains it.

    ## Why this family exists

    The duplicate-cluster family measures paraphrase recall, which is exactly
    what embeddings are good at. Measured on that alone, keyword search looks
    like dead weight — and the conclusion "drop lexical retrieval" would have
    followed from a benchmark that never tested the thing lexical retrieval is
    for.

    This family tests it directly: an error code copied out of a log. An
    embedding model turns ``ERR-40213`` into an unremarkable point somewhere
    near "error", so semantic search has no way to distinguish the right ticket
    from a hundred other error reports. A GIN index on a tsvector finds it
    exactly.

    Derived from the corpus rather than hand-written, so it cannot drift out of
    step with the seed. Codes appearing in more than one ticket are skipped:
    the ground truth has to be unambiguous, and a repeated code would make the
    label wrong rather than merely hard.
    """
    rows = await session.execute(
        select(Ticket.id, Ticket.ticket_number, Ticket.title, Ticket.description).where(
            Ticket.description.op("~")("ERR-[0-9]{5}"),
            Ticket.deleted_at.is_(None),
        )
    )

    by_code: dict[str, list[tuple[uuid.UUID, int, str]]] = {}
    for ticket_id, number, title, description in rows.all():
        for code in set(_ERROR_CODE.findall(description or "")):
            by_code.setdefault(code, []).append((ticket_id, number, title))

    queries: list[EvalQuery] = []
    for index, (code, holders) in enumerate(sorted(by_code.items())):
        if len(holders) != 1:
            continue
        ticket_id, number, title = holders[0]
        phrasing = _IDENTIFIER_PHRASINGS[index % len(_IDENTIFIER_PHRASINGS)]
        queries.append(
            EvalQuery(
                label=f"{code} -> OS-{number}",
                # The query is the *code*, not the ticket. Passing the title
                # here would leak the answer's wording into the question and
                # measure paraphrase matching all over again.
                title=phrasing.format(code=code),
                description=None,
                cluster=f"identifier:{code}",
                relevant=frozenset({ticket_id}),
                # Nothing to exclude: the query is free text, and the one
                # relevant ticket must stay in the candidate set.
                exclude=None,
            )
        )
        if len(queries) >= limit:
            break
    return queries
