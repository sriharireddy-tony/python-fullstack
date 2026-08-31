"""Seed a development tenant with users, teams, clients, and sample tickets.

    cd backend
    .venv\\Scripts\\python -m scripts.seed

Idempotent: re-running creates nothing twice.

Note the two-phase structure. Row-level security is FORCED on tenant-scoped
tables, so an INSERT with no ``app.tenant_id`` set fails the policy's WITH CHECK
- correctly. Unscoped rows (the tenant itself, its counter, the platform admin)
are created first, then the session is re-scoped to that tenant before anything
tenant-owned is written. Seeding therefore exercises the same isolation path as
the application.

Development passwords are printed at the end. This refuses to run in production.
"""

from __future__ import annotations

import asyncio
import sys
import uuid

from app.core.config import settings
from app.core.database import session_scope
from app.core.logging import configure_logging
from app.core.permissions import Role
from app.core.security import hash_password
from app.modules.clients.models import Client, ClientTier
from app.modules.identity.models import User, UserTeam
from app.modules.teams.models import Team
from app.modules.tenancy.models import PlatformAdmin, Tenant, TenantCounter
from app.modules.tickets.enums import (
    Environment,
    Impact,
    Severity,
    TicketStatus,
    Workaround,
    derive_priority,
)
from app.modules.tickets.models import Ticket, TicketStatusHistory
from sqlalchemy import select

TENANT_CODE = "keka"
TENANT_NAME = "Keka"

DEV_PASSWORD = "ChangeMe123!dev"
PLATFORM_ADMIN_PASSWORD = "PlatformAdmin123!dev"
PLATFORM_ADMIN_EMAIL = "platform@os-tracker.local"

TEAMS = [
    ("Platform", "PLAT", "Core platform and infrastructure"),
    ("CoreHR", "CHR", "Employee records, onboarding, org structure"),
    ("Payroll", "PAY", "Payroll processing, payslips, statutory"),
    ("UI", "UI", "Front-end and design system"),
    ("Performance", "PERF", "Goals, reviews, feedback"),
    ("Workforce", "WF", "Attendance, leave, shifts"),
]

CLIENTS = [
    ("Acme Industries", "ACME", ClientTier.ENTERPRISE),
    ("Northwind Trading", "NWND", ClientTier.PREMIUM),
    ("Globex Corporation", "GLBX", ClientTier.STANDARD),
]

USERS = [
    ("admin@keka.local", "Aditi Admin", Role.TENANT_ADMIN, None),
    ("cslead@keka.local", "Chandra CS Lead", Role.CS_LEAD, None),
    ("cs@keka.local", "Ceera CS Agent", Role.CS_AGENT, None),
    ("payroll.mgr@keka.local", "Manoj Manager", Role.TEAM_MANAGER, "Payroll"),
    ("dev1@keka.local", "Divya Developer", Role.DEVELOPER, "Payroll"),
    ("dev2@keka.local", "Dinesh Developer", Role.DEVELOPER, "CoreHR"),
]

SAMPLE_TICKETS = [
    (
        "Payroll export fails for mid-month joiners",
        "Running the monthly payroll export errors out when any employee joined after the 15th.",
        "Acme Industries",
        "Payroll",
        Severity.S1_CRITICAL,
        Impact.DEPARTMENT,
        Workaround.NONE,
    ),
    (
        "Leave balance shows one day short after carry-forward",
        "After the annual carry-forward job, several employees report their "
        "balance is one day lower than expected.",
        "Northwind Trading",
        "Workforce",
        Severity.S2_MAJOR,
        Impact.FEW_USERS,
        Workaround.PAINFUL,
    ),
    (
        "Org chart avatars misaligned on Firefox",
        "Profile pictures overlap the name label in the org chart, but only on Firefox.",
        "Globex Corporation",
        "UI",
        Severity.S4_COSMETIC,
        Impact.FEW_USERS,
        Workaround.EASY,
    ),
]


async def seed() -> None:
    if settings.is_production:
        print("Refusing to seed a production environment.", file=sys.stderr)
        raise SystemExit(1)

    # --- phase 1: unscoped rows -------------------------------------------
    async with session_scope(tenant_id=None) as db:
        tenant = (
            await db.execute(select(Tenant).where(Tenant.code == TENANT_CODE))
        ).scalar_one_or_none()

        if tenant is None:
            tenant = Tenant(name=TENANT_NAME, code=TENANT_CODE, is_active=True)
            db.add(tenant)
            await db.flush()
            db.add(TenantCounter(tenant_id=tenant.id, last_ticket_number=0))
            print(f"created  tenant          {TENANT_NAME}")
        else:
            print(f"exists   tenant          {TENANT_NAME}")

        tenant_id = tenant.id

        admin = (
            await db.execute(
                select(PlatformAdmin).where(PlatformAdmin.email == PLATFORM_ADMIN_EMAIL)
            )
        ).scalar_one_or_none()
        if admin is None:
            db.add(
                PlatformAdmin(
                    email=PLATFORM_ADMIN_EMAIL,
                    full_name="Platform Administrator",
                    password_hash=hash_password(PLATFORM_ADMIN_PASSWORD),
                    is_active=True,
                )
            )
            print("created  platform admin")

    # --- phase 2: tenant-scoped rows --------------------------------------
    # Re-scoped, so every INSERT below satisfies the RLS WITH CHECK clause.
    async with session_scope(tenant_id=tenant_id) as db:
        teams: dict[str, uuid.UUID] = {}
        for name, code, description in TEAMS:
            existing = (
                await db.execute(select(Team).where(Team.code == code))
            ).scalar_one_or_none()
            if existing is None:
                team = Team(
                    tenant_id=tenant_id,
                    name=name,
                    code=code,
                    description=description,
                    is_active=True,
                )
                db.add(team)
                await db.flush()
                teams[name] = team.id
                print(f"created  team            {name}")
            else:
                teams[name] = existing.id

        clients: dict[str, uuid.UUID] = {}
        for name, code, tier in CLIENTS:
            existing = (
                await db.execute(select(Client).where(Client.code == code))
            ).scalar_one_or_none()
            if existing is None:
                client = Client(
                    tenant_id=tenant_id, name=name, code=code, tier=tier, is_active=True
                )
                db.add(client)
                await db.flush()
                clients[name] = client.id
                print(f"created  client          {name}")
            else:
                clients[name] = existing.id

        users: dict[str, uuid.UUID] = {}
        for email, full_name, role, team_name in USERS:
            existing = (
                await db.execute(select(User).where(User.email == email))
            ).scalar_one_or_none()
            if existing is not None:
                users[email] = existing.id
                continue

            user = User(
                tenant_id=tenant_id,
                email=email,
                full_name=full_name,
                role=role,
                password_hash=hash_password(DEV_PASSWORD),
                is_active=True,
                preferences={},
            )
            db.add(user)
            await db.flush()
            users[email] = user.id

            if team_name:
                db.add(UserTeam(user_id=user.id, team_id=teams[team_name], is_primary=True))
            print(f"created  user            {email:<24} {role.value}")

        # Assign the Payroll manager to their team record.
        payroll = await db.get(Team, teams["Payroll"])
        if payroll is not None and payroll.manager_id is None:
            payroll.manager_id = users["payroll.mgr@keka.local"]

        # --- sample tickets ------------------------------------------------
        cs_agent = users["cs@keka.local"]
        existing_tickets = await db.scalar(select(Ticket.id).limit(1))
        if existing_tickets is None:
            for title, description, client_name, team_name, sev, imp, wa in SAMPLE_TICKETS:
                counter = await db.get(TenantCounter, tenant_id)
                assert counter is not None
                counter.last_ticket_number += 1
                await db.flush()

                ticket = Ticket(
                    tenant_id=tenant_id,
                    ticket_number=counter.last_ticket_number,
                    title=title,
                    description=description,
                    environment=Environment.PRODUCTION,
                    client_id=clients[client_name],
                    team_id=teams[team_name],
                    created_by_id=cs_agent,
                    severity=sev,
                    impact=imp,
                    workaround=wa,
                    priority=derive_priority(sev, imp, wa),
                    status=TicketStatus.OPEN,
                    reporter_name="HR Operations",
                )
                db.add(ticket)
                await db.flush()
                db.add(
                    TicketStatusHistory(
                        tenant_id=tenant_id,
                        ticket_id=ticket.id,
                        from_status=None,
                        to_status=TicketStatus.OPEN,
                        changed_by_id=cs_agent,
                        note="Created",
                    )
                )
                print(
                    f"created  ticket          OS-{counter.last_ticket_number} "
                    f"[{derive_priority(sev, imp, wa).value.upper()}] {title[:40]}"
                )

    print("\n" + "=" * 72)
    print("  Sign in at http://localhost:5173")
    print("=" * 72)
    for email, _name, role, _team in USERS:
        print(f"  {email:<26} {role.value:<14} {DEV_PASSWORD}")
    print(f"\n  {PLATFORM_ADMIN_EMAIL:<26} {'platform admin':<14} {PLATFORM_ADMIN_PASSWORD}")
    print("=" * 72)


if __name__ == "__main__":
    configure_logging()
    asyncio.run(seed())
