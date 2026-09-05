"""Generate a realistic demo dataset.

    cd backend
    .venv\\Scripts\\python -m scripts.seed_demo          # add demo data
    .venv\\Scripts\\python -m scripts.seed_demo --reset  # wipe demo tickets first

Builds what the chatbot and the retrieval layer both need: enough tickets, spread
across teams, clients, statuses, and six months of dates, with assignees who
actually belong to the owning team and timestamps that tell a coherent story.

Two deliberate properties beyond "lots of rows":

* **Lifecycle consistency.** A closed ticket has a resolved_at before its
  closed_at, both after created_at, and a status history that walks the real
  transitions. Aggregate questions like "average time to resolve" are only
  meaningful if the timestamps are not random.

* **80 distinct bugs plus 20 near-duplicates.** Every scenario is raised
  exactly once, so no two tickets share their text. The duplicates are the
  twenty hand-written variants in `evals/datasets.py`, each the same defect in
  a different customer's words -- which is what makes them a usable golden set
  for similar-issue retrieval, and what stops the evaluation being measured on
  byte-identical pairs.

Deterministic: a fixed random seed, so re-running produces the same dataset and
measurements stay comparable. Refuses to run against production.
"""

from __future__ import annotations

import argparse
import asyncio
import random
import sys
import uuid
from datetime import UTC, datetime, timedelta

from app.core.config import settings
from app.core.database import session_scope
from app.core.logging import configure_logging
from app.core.permissions import Role
from app.core.security import hash_password
from app.modules.clients.models import Client, ClientTier
from app.modules.identity.models import User, UserTeam
from app.modules.intelligence.eval.datasets import GOLDEN_CLUSTERS, Variant
from app.modules.teams.models import Team
from app.modules.tenancy.models import Tenant, TenantCounter
from app.modules.tickets.enums import (
    Environment,
    RejectionReason,
    TicketStatus,
    WaitingOn,
    derive_priority,
)
from app.modules.tickets.models import Ticket, TicketStatusHistory
from sqlalchemy import delete, func, select

from scripts.seed_detailed import DETAILED_SCENARIOS, DetailedScenario
from scripts.seed_scenarios import SCENARIOS, Scenario

RNG = random.Random(42)
PASSWORD = "ChangeMe123!dev"
TENANT_CODE = "keka"
NL = chr(10)

TICKET_COUNT = 100
HISTORY_DAYS = 180

# --------------------------------------------------------------------- people

#: Two to three developers per team plus a manager, so "who is working on what"
#: and "which team is overloaded" have real answers.
TEAM_STAFF: dict[str, list[tuple[str, str, Role]]] = {
    "Platform": [
        ("platform.mgr@keka.local", "Prakash Menon", Role.TEAM_MANAGER),
        ("arjun.dev@keka.local", "Arjun Rao", Role.DEVELOPER),
        ("nisha.dev@keka.local", "Nisha Verma", Role.DEVELOPER),
        ("kiran.dev@keka.local", "Kiran Bose", Role.DEVELOPER),
    ],
    "CoreHR": [
        ("corehr.mgr@keka.local", "Meera Iyer", Role.TEAM_MANAGER),
        ("rohit.dev@keka.local", "Rohit Kumar", Role.DEVELOPER),
        ("sneha.dev@keka.local", "Sneha Pillai", Role.DEVELOPER),
    ],
    "Payroll": [
        ("payroll.mgr2@keka.local", "Naveen Gupta", Role.TEAM_MANAGER),
        ("kavya.dev@keka.local", "Kavya Nair", Role.DEVELOPER),
        ("rahul.dev@keka.local", "Rahul Shetty", Role.DEVELOPER),
        ("fatima.dev@keka.local", "Fatima Sheikh", Role.DEVELOPER),
    ],
    "UI": [
        ("ui.mgr@keka.local", "Anita Desai", Role.TEAM_MANAGER),
        ("vikram.dev@keka.local", "Vikram Singh", Role.DEVELOPER),
        ("priya.dev@keka.local", "Priya Krishnan", Role.DEVELOPER),
    ],
    "Performance": [
        ("perf.mgr@keka.local", "Suresh Babu", Role.TEAM_MANAGER),
        ("anjali.dev@keka.local", "Anjali Mehta", Role.DEVELOPER),
        ("tarun.dev@keka.local", "Tarun Joshi", Role.DEVELOPER),
        # A deliberate first-name collision with Priya Krishnan on the UI team.
        #
        # Earlier revisions of this script *removed* colliding names, because
        # the chatbot had no way to resolve them and would answer about
        # whichever it found. That was the right call then and the wrong one
        # now: the assistant has a `find_people` tool that returns every match
        # and flags ambiguity, and a corpus with no ambiguous names never
        # exercises it. Two people sharing a first name is also simply what
        # companies look like.
        ("priya.s@keka.local", "Priya Sharma", Role.DEVELOPER),
    ],
    "Workforce": [
        ("workforce.mgr@keka.local", "Latha Reddy", Role.TEAM_MANAGER),
        ("imran.dev@keka.local", "Imran Qureshi", Role.DEVELOPER),
        ("deepa.dev@keka.local", "Deepa Bhat", Role.DEVELOPER),
        ("santosh.dev@keka.local", "Santosh Patil", Role.DEVELOPER),
    ],
}

#: Demo accounts from earlier revisions of this script, soft-deleted on every
#: run so it is self-healing rather than leaving orphans behind.
#:
#: These are retired because they are *stale*, not because they collide: a
#: deliberate collision now exists (two people called Priya) so the
#: assistant's name-disambiguation path is exercised by real data.
RETIRED_EMAILS = [
    "divya.dev@keka.local",
    "dinesh.dev@keka.local",
]

EXTRA_CS = [
    ("cs2@keka.local", "Farah Ahmed", Role.CS_AGENT),
    ("cs3@keka.local", "Gopal Krishnan", Role.CS_AGENT),
]

EXTRA_CLIENTS = [
    ("Initech Systems", "INTC", ClientTier.PREMIUM),
    ("Umbrella Retail", "UMBR", ClientTier.ENTERPRISE),
    ("Soylent Foods", "SOYL", ClientTier.STANDARD),
    ("Stark Manufacturing", "STRK", ClientTier.ENTERPRISE),
    ("Wayne Logistics", "WAYN", ClientTier.PREMIUM),
    ("Cyberdyne Labs", "CYBR", ClientTier.STANDARD),
    ("Tyrell Consulting", "TYRL", ClientTier.STANDARD),
]

#: The planted near-duplicate clusters, imported rather than defined here.
#:
#: The evaluation module owns them because it is the consumer that breaks if
#: they drift: two lists that must agree eventually disagree, and the symptom
#: would be a golden set that silently shrinks.
DUPLICATE_PARAPHRASES: dict[str, tuple[Variant, ...]] = GOLDEN_CLUSTERS


#: Status mix for a tracker that has been running six months. Weighted so most
#: history is closed, with a realistic live backlog on top.
STATUS_WEIGHTS: list[tuple[TicketStatus, int]] = [
    (TicketStatus.CLOSED, 52),
    (TicketStatus.IN_PROGRESS, 12),
    (TicketStatus.OPEN, 12),
    (TicketStatus.ASSIGNED, 8),
    (TicketStatus.RESOLVED, 6),
    (TicketStatus.REJECTED, 4),
    (TicketStatus.ON_HOLD, 6),
]

RESOLUTION_NOTES = [
    "Root cause was a missing null check in the calculation service. Covered by a test.",
    "The scheduled job used the wrong timezone. Corrected and re-ran for the period.",
    "Cache was not invalidated after the update. Added explicit invalidation on write.",
    "Query was missing a tenant filter in the reporting path. Corrected and verified.",
    "Rounding was applied before aggregation instead of after. Reordered the operations.",
    "The API contract changed upstream. Updated the client and added a version check.",
]

REJECTION_NOTES = [
    "Working as designed. The behaviour follows the configured policy for this client.",
    "Could not reproduce on the current build after three attempts. Closing pending more detail.",
    "Same underlying issue as an earlier ticket, tracked there instead.",
    "Client-side configuration rather than a product defect. Guided the admin through it.",
]


#: Real bug reports carry literal identifiers: an error code copied from a log,
#: an employee code, a failed job id. The first version of this seed had almost
#: none, and that turned out to matter a great deal -- the retrieval ablation
#: could only measure paraphrase matching, which is precisely what embeddings
#: are best at, so it had nothing to say about whether keyword search earns its
#: place. A corpus of pure prose is not a realistic bug tracker.
#:
#: Applied uniformly across every scenario, deliberately. Attaching codes only
#: to the planted duplicate clusters would hand keyword search the benchmark by
#: construction.
IDENTIFIER_LINES: list[str] = [
    "Log shows {code} on the request to /api/v3/{module}/records.",
    "Browser console reports {code} when the page loads.",
    "Affected employee code {emp}. Trace id {trace}.",
    "Background job {run} failed with {code}.",
    "Support bundle references {code}; correlation id {trace}.",
]

#: Fraction of tickets that carry an identifier. Not all of them: plenty of real
#: reports are prose only, and a corpus where every ticket has a unique code
#: would make keyword search look better than it is.
IDENTIFIER_SHARE = 0.45

MODULE_SLUGS = {
    "Payroll": "payroll",
    "CoreHR": "corehr",
    "Platform": "platform",
    "UI": "web",
    "Performance": "performance",
    "Workforce": "attendance",
}


def identifier_line(team_name: str, sequence: int) -> str | None:
    """A realistic identifier line for one ticket, or None.

    The error code is unique per ticket by construction, which is what makes
    the evaluation's identifier query family derivable from the data instead of
    hand-maintained -- and hand-maintained fixtures drift from the seed.
    """
    if RNG.random() >= IDENTIFIER_SHARE:
        return None
    template = RNG.choice(IDENTIFIER_LINES)
    return template.format(
        code=f"ERR-{40000 + sequence}",
        module=MODULE_SLUGS.get(team_name, "platform"),
        emp=f"EMP-{RNG.randint(1000, 9999)}",
        trace=f"{RNG.getrandbits(48):012x}",
        run=f"JOB-{RNG.randint(100, 999)}",
    )


def weighted_status() -> TicketStatus:
    population = [status for status, weight in STATUS_WEIGHTS for _ in range(weight)]
    return RNG.choice(population)


def _describe(body: str, identifier: str | None) -> str:
    """Append the identifier line, if this ticket got one.

    Appended rather than woven in, because that is how it arrives in reality:
    the reporter describes the problem and then pastes what the log said.
    """
    if not identifier:
        return body
    return body + "\n\n" + identifier


PlanItem = tuple[Scenario, Variant | None, DetailedScenario | None]


def build_ticket_plan(count: int, profile: str = "full") -> list[PlanItem]:
    """Every scenario exactly once, plus the near-duplicate variants.

    ## Why there is no sampling any more

    This used to draw `count` tickets from the scenario list weighted by
    severity, which meant popular scenarios were raised many times **with their
    text reused verbatim**. The result was 220 tickets over 64 scenarios, nine
    of which shared a title word for word, and 200 of the 220 sitting in an
    exact-duplicate group.

    Two things were wrong with that. It is not what a bug tracker looks like --
    the same customer does not file the identical sentence nine times. And it
    quietly flattered the retrieval evaluation, because a "paraphrase" cluster
    whose members are byte-identical measures exact matching, which both
    retrievers solve perfectly.

    So the plan is now deterministic: 80 distinct bugs, each once, plus the 20
    hand-written variants from the golden clusters. 100 tickets, zero verbatim
    repeats, and the only duplicates in the corpus are the ones whose ground
    truth is recorded.

    The priority mix is therefore set by the severities in
    `seed_scenarios.py` rather than by sampling weights -- currently 9% P1,
    36% P2, 35% P3, 21% P4, which is the shape a healthy tracker has.
    """
    if profile == "ten":
        # The inspection corpus: ten hand-written tickets with full write-ups
        # and their own reproduction steps. No duplicates, no sampling -- see
        # `scripts/seed_detailed.py` for why this is a separate corpus rather
        # than a smaller slice of the benchmark one.
        return [
            (
                (d.team, d.title, d.description, d.severity, d.impact, d.workaround),
                None,
                d,
            )
            for d in DETAILED_SCENARIOS
        ]

    plan: list[PlanItem] = []
    by_title = {scenario[1]: scenario for scenario in SCENARIOS}

    # Every distinct bug, once.
    plan.extend((scenario, None, None) for scenario in SCENARIOS)

    # Then the variants: the same defect, worded as another customer reported
    # it. A variant carries its own title *and* description.
    for base_title, variants in DUPLICATE_PARAPHRASES.items():
        base = by_title[base_title]
        plan.extend((base, variant, None) for variant in variants)

    if len(plan) != count:
        # Loud rather than silent. A mismatch means the scenario list and the
        # requested count have drifted apart, and quietly truncating would hide
        # which bugs went missing -- the exact class of problem this rewrite
        # exists to remove.
        print(
            f"note: {len(plan)} tickets available "
            f"({len(SCENARIOS)} distinct + "
            f"{sum(len(v) for v in DUPLICATE_PARAPHRASES.values())} variants), "
            f"--count asked for {count}"
        )

    # Shuffled so creation order does not correlate with team or severity;
    # seeded RNG, so the shuffle is the same on every run.
    RNG.shuffle(plan)
    return plan[:count] if count < len(plan) else plan


async def reset_demo_tickets(tenant_id: uuid.UUID) -> None:
    async with session_scope(tenant_id=tenant_id) as db:
        await db.execute(delete(TicketStatusHistory))
        await db.execute(delete(Ticket))
        counter = await db.get(TenantCounter, tenant_id)
        if counter is not None:
            counter.last_ticket_number = 0
    print("reset: removed existing tickets and history")


async def seed() -> None:
    if settings.is_production:
        print("Refusing to seed a production environment.", file=sys.stderr)
        raise SystemExit(1)

    parser = argparse.ArgumentParser()
    parser.add_argument("--reset", action="store_true", help="delete existing tickets first")
    parser.add_argument("--count", type=int, default=TICKET_COUNT)
    parser.add_argument(
        "--profile",
        choices=("full", "ten"),
        default="full",
        help=(
            "full: 100 tickets with known duplicate clusters, for evaluation. "
            "ten: 10 long hand-written tickets with no duplicates, for working "
            "on the index by hand."
        ),
    )
    args = parser.parse_args()

    async with session_scope(tenant_id=None) as db:
        tenant = (
            await db.execute(select(Tenant).where(Tenant.code == TENANT_CODE))
        ).scalar_one_or_none()
        if tenant is None:
            print("Run `python -m scripts.seed` first to create the tenant.", file=sys.stderr)
            raise SystemExit(1)
        tenant_id = tenant.id

    if args.reset:
        await reset_demo_tickets(tenant_id)

    async with session_scope(tenant_id=tenant_id) as db:
        teams = {
            t.name: t
            for t in (await db.execute(select(Team).where(Team.deleted_at.is_(None))))
            .scalars()
            .all()
        }
        if not teams:
            print("No teams found. Run `python -m scripts.seed` first.", file=sys.stderr)
            raise SystemExit(1)

        # ---- clients -----------------------------------------------------
        for name, code, tier in EXTRA_CLIENTS:
            exists = (
                await db.execute(select(Client).where(Client.code == code))
            ).scalar_one_or_none()
            if exists is None:
                db.add(Client(tenant_id=tenant_id, name=name, code=code, tier=tier, is_active=True))
        await db.flush()
        clients = list(
            (await db.execute(select(Client).where(Client.deleted_at.is_(None)))).scalars().all()
        )
        print(f"clients: {len(clients)}")

        # ---- staff -------------------------------------------------------
        team_members: dict[str, list[User]] = {}
        created_users = 0

        for team_name, staff in TEAM_STAFF.items():
            team = teams.get(team_name)
            if team is None:
                continue
            members: list[User] = []
            for email, full_name, role in staff:
                user = (
                    await db.execute(select(User).where(User.email == email))
                ).scalar_one_or_none()
                if user is not None and user.full_name != full_name:
                    # Names are corrected on re-run; emails are the stable key.
                    user.full_name = full_name
                if user is None:
                    user = User(
                        tenant_id=tenant_id,
                        email=email,
                        full_name=full_name,
                        role=role,
                        password_hash=hash_password(PASSWORD),
                        is_active=True,
                        preferences={},
                    )
                    db.add(user)
                    await db.flush()
                    db.add(UserTeam(user_id=user.id, team_id=team.id, is_primary=True))
                    created_users += 1
                    if role is Role.TEAM_MANAGER and team.manager_id is None:
                        team.manager_id = user.id
                members.append(user)
            # Only developers take assignments; managers assign rather than own.
            team_members[team_name] = [m for m in members if m.role is Role.DEVELOPER]

        for email, full_name, role in EXTRA_CS:
            if (
                await db.execute(select(User).where(User.email == email))
            ).scalar_one_or_none() is None:
                db.add(
                    User(
                        tenant_id=tenant_id,
                        email=email,
                        full_name=full_name,
                        role=role,
                        password_hash=hash_password(PASSWORD),
                        is_active=True,
                        preferences={},
                    )
                )
                created_users += 1
        await db.flush()
        print(f"users: {created_users} created")

        # ---- retire superseded demo accounts ----------------------------
        for email in RETIRED_EMAILS:
            stale = (await db.execute(select(User).where(User.email == email))).scalar_one_or_none()
            if stale is not None and stale.deleted_at is None:
                stale.is_active = False
                stale.deleted_at = datetime.now(UTC)
                print(f"retired  user            {email}")
        await db.flush()

        cs_users = list(
            (await db.execute(select(User).where(User.role.in_([Role.CS_AGENT, Role.CS_LEAD]))))
            .scalars()
            .all()
        )

        # ---- tickets -----------------------------------------------------
        #
        # Idempotent, like every other section of this script. It was not, and
        # the asymmetry caused a real problem: re-running without `--reset`
        # merely to add one user *also* created another 220 tickets, silently
        # doubling the corpus and breaking the AI layer's "every ticket has an
        # embedding" invariant. Users, teams, and clients were create-if-missing
        # while tickets were create-always, which is not a distinction anyone
        # would guess from the command line.
        existing_tickets = int(
            await db.scalar(
                select(func.count()).select_from(Ticket).where(Ticket.deleted_at.is_(None))
            )
            or 0
        )
        if args.profile == "full" and existing_tickets >= args.count:
            print(
                f"tickets: {existing_tickets} already present, leaving them alone "
                f"(use --reset to rebuild)"
            )
            return

        counter = await db.get(TenantCounter, tenant_id)
        assert counter is not None

        now = datetime.now(UTC)
        # Top up to the target rather than always creating a full batch, so a
        # partially-seeded database converges instead of overshooting.
        target = 10 if args.profile == "ten" else args.count - existing_tickets
        plan = build_ticket_plan(target, profile=args.profile)
        status_tally: dict[str, int] = {}

        for sequence, (scenario, variant, detail) in enumerate(plan, start=1):
            team_name, title, description, severity, impact, workaround = scenario
            team = teams[team_name]
            client = RNG.choice(clients)
            creator = RNG.choice(cs_users)
            status = weighted_status()

            # Older tickets are more likely to be finished; a six-month-old
            # ticket still sitting Open would be unrealistic.
            age_days = RNG.randint(1, HISTORY_DAYS)
            if age_days > 90 and status in (TicketStatus.OPEN, TicketStatus.ASSIGNED):
                status = TicketStatus.CLOSED
            if age_days < 5 and status is TicketStatus.CLOSED:
                status = TicketStatus.IN_PROGRESS

            created_at = now - timedelta(days=age_days, hours=RNG.randint(0, 23))
            counter.last_ticket_number += 1

            assignee = None
            if status is not TicketStatus.OPEN:
                candidates = team_members.get(team_name) or []
                assignee = RNG.choice(candidates) if candidates else None
                if assignee is None:
                    status = TicketStatus.OPEN

            resolved_at = closed_at = None
            resolution_notes = rejection_reason = None
            waiting_on = None
            reopen_count = 0

            if status in (TicketStatus.RESOLVED, TicketStatus.REJECTED, TicketStatus.CLOSED):
                resolved_at = created_at + timedelta(hours=RNG.randint(4, 24 * min(age_days, 21)))
                if status is TicketStatus.REJECTED:
                    rejection_reason = RNG.choice(list(RejectionReason))
                else:
                    resolution_notes = RNG.choice(RESOLUTION_NOTES)
                if status is TicketStatus.CLOSED:
                    closed_at = resolved_at + timedelta(hours=RNG.randint(1, 72))
                    resolution_notes = resolution_notes or RNG.choice(RESOLUTION_NOTES)
                    # A minority come back — the reopen-rate signal needs to exist
                    # for "which team has the most reopens" to be answerable.
                    if RNG.random() < 0.12:
                        reopen_count = RNG.choice([1, 1, 1, 2])
            elif status is TicketStatus.ON_HOLD:
                waiting_on = RNG.choice(list(WaitingOn))

            ticket = Ticket(
                tenant_id=tenant_id,
                ticket_number=counter.last_ticket_number,
                title=variant[0] if variant else title,
                description=_describe(
                    variant[1] if variant else description,
                    identifier_line(team_name, sequence),
                ),
                steps_to_reproduce=(
                    detail.steps
                    if detail
                    else (
                        "1. Sign in as an HR admin"
                        + NL
                        + f"2. Open the {team_name} module"
                        + NL
                        + "3. Perform the action described above"
                        + NL
                        + "4. Observe the incorrect result"
                    )
                ),
                expected_result=(
                    detail.expected
                    if detail
                    else "The operation completes and the values shown are correct."
                ),
                actual_result=(
                    detail.actual if detail else "The operation fails or shows incorrect values."
                ),
                environment=RNG.choices(
                    [Environment.PRODUCTION, Environment.STAGING, Environment.UAT],
                    weights=[80, 12, 8],
                )[0],
                client_id=client.id,
                reporter_name=RNG.choice(
                    ["HR Operations", "Payroll Team", "IT Helpdesk", "People Ops", "Finance"]
                ),
                created_by_id=creator.id,
                team_id=team.id,
                assignee_id=assignee.id if assignee else None,
                severity=severity,
                impact=impact,
                workaround=workaround,
                priority=derive_priority(severity, impact, workaround),
                status=status,
                on_hold_waiting_on=waiting_on,
                resolution_notes=resolution_notes,
                rejection_reason=rejection_reason,
                reopen_count=reopen_count,
                resolved_at=resolved_at,
                closed_at=closed_at,
                created_at=created_at,
                updated_at=closed_at or resolved_at or created_at,
                version=1,
            )
            db.add(ticket)
            await db.flush()

            # ---- a status history that walks the real transitions --------
            walk: list[tuple[TicketStatus | None, TicketStatus, datetime, uuid.UUID]] = [
                (None, TicketStatus.OPEN, created_at, creator.id)
            ]
            cursor = created_at
            actor = assignee.id if assignee else creator.id

            if status is not TicketStatus.OPEN:
                cursor += timedelta(hours=RNG.randint(1, 20))
                walk.append((TicketStatus.OPEN, TicketStatus.ASSIGNED, cursor, actor))

            if status in (
                TicketStatus.IN_PROGRESS,
                TicketStatus.ON_HOLD,
                TicketStatus.RESOLVED,
                TicketStatus.REJECTED,
                TicketStatus.CLOSED,
            ):
                cursor += timedelta(hours=RNG.randint(1, 30))
                walk.append((TicketStatus.ASSIGNED, TicketStatus.IN_PROGRESS, cursor, actor))

            if status is TicketStatus.ON_HOLD:
                cursor += timedelta(hours=RNG.randint(1, 40))
                walk.append((TicketStatus.IN_PROGRESS, TicketStatus.ON_HOLD, cursor, actor))

            if status in (TicketStatus.RESOLVED, TicketStatus.CLOSED) and resolved_at:
                walk.append((TicketStatus.IN_PROGRESS, TicketStatus.RESOLVED, resolved_at, actor))
            if status is TicketStatus.REJECTED and resolved_at:
                walk.append((TicketStatus.IN_PROGRESS, TicketStatus.REJECTED, resolved_at, actor))
            if status is TicketStatus.CLOSED and closed_at:
                walk.append((TicketStatus.RESOLVED, TicketStatus.CLOSED, closed_at, creator.id))

            for from_status, to_status, at, by in walk:
                db.add(
                    TicketStatusHistory(
                        tenant_id=tenant_id,
                        ticket_id=ticket.id,
                        from_status=from_status,
                        to_status=to_status,
                        changed_by_id=by,
                        changed_at=at,
                        note=None,
                    )
                )

            status_tally[status.value] = status_tally.get(status.value, 0) + 1

        await db.flush()

    # ---- summary ---------------------------------------------------------
    async with session_scope(tenant_id=tenant_id) as db:
        total = await db.scalar(
            select(func.count()).select_from(Ticket).where(Ticket.deleted_at.is_(None))
        )
        print(f"\ntickets: {total} total")
        print("\nby status:")
        for status_name, count in sorted(status_tally.items(), key=lambda kv: -kv[1]):
            print(f"  {status_name:<14} {count}")

        print("\nby team:")
        rows = await db.execute(
            select(Team.name, func.count(Ticket.id))
            .join(Ticket, Ticket.team_id == Team.id)
            .where(Ticket.deleted_at.is_(None))
            .group_by(Team.name)
            .order_by(func.count(Ticket.id).desc())
        )
        for name, count in rows.all():
            print(f"  {name:<14} {count}")

        print("\nby priority:")
        rows = await db.execute(
            select(Ticket.priority, func.count())
            .where(Ticket.deleted_at.is_(None))
            .group_by(Ticket.priority)
            .order_by(Ticket.priority)
        )
        for priority, count in rows.all():
            print(f"  {priority.value:<14} {count}")

    print(f"\nAll demo accounts use the password: {PASSWORD}")


if __name__ == "__main__":
    configure_logging()
    asyncio.run(seed())
