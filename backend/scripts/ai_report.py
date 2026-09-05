r"""What the AI layer cost, and whether it was worth it.

    .venv\Scripts\python -m scripts.ai_report
    .venv\Scripts\python -m scripts.ai_report --days 7

Reads `ai_usage`, `ai_suggestions`, and `ai_analysis_runs`. No model calls, so
it is free to run as often as you like.

## Why acceptance rate is the headline and not recall

Every other number in this project describes what the system *did*: recall@20,
precision@5, tokens, latency, turns. They are necessary and they are all
proxies. The only number that says whether the work was worth doing is whether
the people triaging bugs agreed with it — an accept, a reject, a thumbs up.

That is also why the accept and reject buttons exist at all rather than the
panels being read-only: the feature and its own evaluation data are the same
thing. A suggestion nobody was asked about produces no signal.

**Cost per accepted suggestion** is the number to optimise. Cost per call
rewards making calls cheaper; cost per *accepted* suggestion rewards making
them useful, and a cheaper call that nobody accepts moves it the wrong way.
"""

from __future__ import annotations

import argparse
import asyncio
import uuid
from datetime import UTC, datetime, timedelta

from app.core.database import session_scope
from app.core.logging import configure_logging
from app.modules.intelligence.models import (
    AiAnalysisRun,
    AiSuggestion,
    AiUsage,
    AnalysisStatus,
    SuggestionStatus,
)
from app.modules.tenancy.models import Tenant
from sqlalchemy import func, select


def table(header: list[str], rows: list[list[str]]) -> str:
    if not rows:
        return "  (nothing recorded)"
    widths = [max(len(header[i]), *(len(r[i]) for r in rows)) for i in range(len(header))]
    lines = [
        "  " + "  ".join(cell.ljust(widths[i]) for i, cell in enumerate(header)),
        "  " + "  ".join("-" * w for w in widths),
    ]
    lines += ["  " + "  ".join(cell.ljust(widths[i]) for i, cell in enumerate(r)) for r in rows]
    return "\n".join(lines)


async def usage_by_feature(tenant_id: uuid.UUID, since: datetime) -> None:
    """Cost and latency per feature.

    Grouped by feature rather than by model, because "what does the
    similar-issues panel cost" is the question someone actually asks. Grouping
    by model answers "what does Gemini cost", which nobody needs to know in
    isolation.
    """
    async with session_scope(tenant_id=None) as db:
        rows = await db.execute(
            select(
                AiUsage.feature,
                AiUsage.model,
                func.count().label("calls"),
                func.sum(AiUsage.input_tokens),
                func.sum(AiUsage.output_tokens),
                func.sum(AiUsage.estimated_cost),
                func.avg(AiUsage.latency_ms),
                func.count().filter(AiUsage.succeeded.is_(False)),
            )
            .where(AiUsage.created_at >= since)
            .group_by(AiUsage.feature, AiUsage.model)
            .order_by(func.sum(AiUsage.estimated_cost).desc())
        )
        records = rows.all()

    print("\nCALLS BY FEATURE AND MODEL")
    print(
        table(
            ["feature", "model", "calls", "in", "out", "cost", "avg ms", "failed"],
            [
                [
                    feature,
                    model,
                    str(calls),
                    str(int(tokens_in or 0)),
                    str(int(tokens_out or 0)),
                    f"{float(cost or 0):.5f}",
                    f"{float(latency or 0):.0f}",
                    str(failed),
                ]
                for feature, model, calls, tokens_in, tokens_out, cost, latency, failed in records
            ],
        )
    )

    total_cost = sum(float(r[5] or 0) for r in records)
    total_calls = sum(int(r[2]) for r in records)
    failed = sum(int(r[7]) for r in records)
    print(f"\n  {total_calls} call(s), {failed} failed, {total_cost:.5f} total")
    return None


async def suggestion_outcomes(tenant_id: uuid.UUID, since: datetime) -> tuple[int, int, int]:
    """Accept, reject, pending — the signal the whole layer is judged on."""
    async with session_scope(tenant_id=tenant_id) as db:
        rows = await db.execute(
            select(AiSuggestion.status, func.count())
            .where(AiSuggestion.created_at >= since)
            .group_by(AiSuggestion.status)
        )
        # Built by iteration rather than `dict(rows.all())`: the checker cannot
        # infer key and value types through a `Row` sequence, and an annotated
        # empty dict filled in a loop satisfies both tools without a suppression.
        counts: dict[SuggestionStatus, int] = {}
        for status, count in rows.all():
            counts[status] = count

        # Accuracy by confidence band. The question this answers is whether the
        # model's confidence means anything -- if the 0.9+ band is accepted at
        # the same rate as the 0.6 band, the number is decoration and the
        # confidence floor is set on a fiction.
        bands = await db.execute(
            select(
                func.width_bucket(AiSuggestion.confidence, 0.5, 1.0, 5).label("band"),
                func.count(),
                func.count().filter(AiSuggestion.status == SuggestionStatus.ACCEPTED),
            )
            .where(AiSuggestion.created_at >= since, AiSuggestion.confidence.is_not(None))
            .group_by("band")
            .order_by("band")
        )
        by_band = bands.all()

    accepted = counts.get(SuggestionStatus.ACCEPTED, 0)
    rejected = counts.get(SuggestionStatus.REJECTED, 0)
    pending = counts.get(SuggestionStatus.PENDING, 0)

    print("\nSUGGESTION OUTCOMES")
    decided = accepted + rejected
    rate = f"{accepted / decided:.1%}" if decided else "n/a"
    print(
        table(
            ["accepted", "rejected", "pending", "acceptance rate"],
            [[str(accepted), str(rejected), str(pending), rate]],
        )
    )
    if not decided:
        print(
            "\n  Nothing decided yet, so there is no accuracy signal. Every retrieval\n"
            "  metric in this project is a proxy for this number."
        )

    if by_band:
        print("\n  BY CONFIDENCE BAND (is the model's confidence meaningful?)")
        print(
            table(
                ["band", "suggested", "accepted", "rate"],
                [
                    [
                        f"{0.5 + (int(band or 1) - 1) * 0.1:.1f}+",
                        str(total),
                        str(ok),
                        f"{ok / total:.0%}" if total else "n/a",
                    ]
                    for band, total, ok in by_band
                ],
            )
        )

    return accepted, rejected, pending


async def analysis_outcomes(tenant_id: uuid.UUID, since: datetime) -> None:
    async with session_scope(tenant_id=tenant_id) as db:
        rows = await db.execute(
            select(
                AiAnalysisRun.status,
                func.count(),
                func.avg(AiAnalysisRun.turns),
                func.avg(AiAnalysisRun.tool_calls),
                func.avg(AiAnalysisRun.latency_ms),
                func.sum(AiAnalysisRun.estimated_cost),
                func.count().filter(AiAnalysisRun.helpful.is_(True)),
                func.count().filter(AiAnalysisRun.helpful.is_(False)),
            )
            .where(AiAnalysisRun.created_at >= since)
            .group_by(AiAnalysisRun.status)
        )
        records = rows.all()

    print("\nANALYSIS RUNS")
    print(
        table(
            ["status", "runs", "avg turns", "avg tools", "avg ms", "cost", "helpful", "not"],
            [
                [
                    status.value,
                    str(count),
                    f"{float(turns or 0):.1f}",
                    f"{float(tools or 0):.1f}",
                    f"{float(latency or 0):.0f}",
                    f"{float(cost or 0):.5f}",
                    str(up),
                    str(down),
                ]
                for status, count, turns, tools, latency, cost, up, down in records
            ],
        )
    )

    stopped_early = {AnalysisStatus.TIMED_OUT, AnalysisStatus.BUDGET_EXCEEDED}
    partial = sum(int(r[1]) for r in records if r[0] in stopped_early)
    if partial:
        print(
            f"\n  {partial} run(s) stopped at a limit. That is the limits working, not a\n"
            "  fault -- but a rising share means the caps are too tight for the work."
        )


async def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--tenant", default="keka")
    parser.add_argument("--days", type=int, default=30)
    args = parser.parse_args()

    configure_logging()
    async with session_scope(tenant_id=None) as db:
        tenant_id = await db.scalar(select(Tenant.id).where(Tenant.code == args.tenant))
    if tenant_id is None:
        raise SystemExit(f"no tenant with code {args.tenant!r}")

    since = datetime.now(UTC) - timedelta(days=args.days)
    print("=" * 78)
    print(f"AI USAGE REPORT  (tenant={args.tenant}, last {args.days} day(s))")
    print("=" * 78)

    await usage_by_feature(tenant_id, since)
    accepted, _rejected, _pending = await suggestion_outcomes(tenant_id, since)
    await analysis_outcomes(tenant_id, since)

    # The headline number.
    async with session_scope(tenant_id=None) as db:
        total_cost = float(
            await db.scalar(
                select(func.coalesce(func.sum(AiUsage.estimated_cost), 0.0)).where(
                    AiUsage.created_at >= since
                )
            )
            or 0.0
        )

    print(f"\n{'=' * 78}")
    if accepted:
        print(
            f"COST PER ACCEPTED SUGGESTION: {total_cost / accepted:.5f}"
            f"  ({total_cost:.5f} over {accepted} accepted)"
        )
        print(
            "\nThis is the number worth optimising. Cost per *call* rewards making calls\n"
            "cheaper; cost per accepted suggestion rewards making them useful, and a\n"
            "cheaper call nobody accepts moves it the wrong way."
        )
    else:
        print(
            f"TOTAL COST: {total_cost:.5f}, with nothing accepted yet.\n\n"
            "Until people start accepting or rejecting, every quality number here is a\n"
            "proxy. The accept and reject buttons are the measurement instrument."
        )
    print("=" * 78)


if __name__ == "__main__":
    asyncio.run(main())
