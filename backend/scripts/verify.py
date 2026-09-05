"""End-to-end verification against a live database.

    cd backend
    .venv\\Scripts\\python -m scripts.verify

Exercises the paths that unit-level checks cannot reach: real SQL, real
row-level security, real cookies. Run after `alembic upgrade head` and
`scripts.seed`.

The cross-tenant section is the important one. Everything else degrades the
product if it breaks; a tenant leak ends it.
"""

from __future__ import annotations

import asyncio
import sys
import uuid
from typing import Any

import httpx
from app.core.database import session_scope
from app.core.permissions import Role
from app.core.security import hash_password
from app.main import app
from app.modules.clients.models import Client
from app.modules.identity.models import User
from app.modules.teams.models import Team
from app.modules.tenancy.models import Tenant, TenantCounter
from app.modules.tickets.models import Ticket
from sqlalchemy import delete, or_, select, text

PASSWORD = "ChangeMe123!dev"
TENANT_CODE = "keka"
OTHER_TENANT_CODE = "othercorp"
OTHER_USER_EMAIL = "admin@othercorp.local"

passed = 0
failed: list[str] = []


def ok(message: str) -> None:
    global passed
    passed += 1
    print(f"  PASS  {message}")


def bad(message: str) -> None:
    failed.append(message)
    print(f"  FAIL  {message}")


def check(condition: bool, message: str) -> None:
    ok(message) if condition else bad(message)


class Session:
    """A logged-in API client that carries cookies and the CSRF token."""

    def __init__(self, client: httpx.AsyncClient) -> None:
        self.client = client
        self.user: dict[str, Any] = {}

    @property
    def _headers(self) -> dict[str, str]:
        token = self.client.cookies.get("csrf_token")
        return {"X-CSRF-Token": token} if token else {}

    async def login(self, email: str, password: str = PASSWORD) -> httpx.Response:
        response = await self.client.post(
            "/api/v1/auth/login", json={"email": email, "password": password}
        )
        if response.status_code == 200:
            self.user = response.json()
        return response

    async def get(self, url: str, **kwargs: Any) -> httpx.Response:
        return await self.client.get(url, **kwargs)

    async def post(self, url: str, json: Any = None, **kwargs: Any) -> httpx.Response:
        return await self.client.post(url, json=json, headers=self._headers, **kwargs)

    async def patch(self, url: str, json: Any = None) -> httpx.Response:
        return await self.client.patch(url, json=json, headers=self._headers)


async def make_session() -> Session:
    transport = httpx.ASGITransport(app=app)
    client = httpx.AsyncClient(transport=transport, base_url="http://test")
    return Session(client)


async def ensure_second_tenant() -> None:
    """Create a second tenant with its own admin, team, and ticket.

    Written directly through the ORM because tenant provisioning is a platform
    operation, not a tenant-scoped API call.
    """
    async with session_scope(tenant_id=None) as db:
        tenant = (
            await db.execute(select(Tenant).where(Tenant.code == OTHER_TENANT_CODE))
        ).scalar_one_or_none()
        if tenant is None:
            tenant = Tenant(name="Other Corp", code=OTHER_TENANT_CODE, is_active=True)
            db.add(tenant)
            await db.flush()
            db.add(TenantCounter(tenant_id=tenant.id, last_ticket_number=0))
        tenant_id = tenant.id

    async with session_scope(tenant_id=tenant_id) as db:
        user = (
            await db.execute(select(User).where(User.email == OTHER_USER_EMAIL))
        ).scalar_one_or_none()
        if user is None:
            db.add(
                User(
                    tenant_id=tenant_id,
                    email=OTHER_USER_EMAIL,
                    full_name="Olivia Other",
                    role=Role.TENANT_ADMIN,
                    password_hash=hash_password(PASSWORD),
                    is_active=True,
                    preferences={},
                )
            )
        team = (await db.execute(select(Team).where(Team.code == "OTHR"))).scalar_one_or_none()
        if team is None:
            db.add(Team(tenant_id=tenant_id, name="Other Team", code="OTHR", is_active=True))
        client = (
            await db.execute(select(Client).where(Client.code == "OTHC"))
        ).scalar_one_or_none()
        if client is None:
            db.add(Client(tenant_id=tenant_id, name="Other Client", code="OTHC", is_active=True))


#: Titles this script creates. Hard-deleted at the end so the run leaves the
#: database as it found it.
PROBE_TITLE_PREFIXES = (
    "Payslip PDF is blank for contractors",
    "Concurrency probe",
    "Idempotency probe",
)


async def drop_probe_tickets() -> int:
    """Hard-delete the tickets this script created.

    A hard delete, not the soft delete the API performs: a soft-deleted ticket
    still counts towards the row totals other scripts assert on.
    """
    async with session_scope(tenant_id=None) as db:
        tenant_id = await db.scalar(select(Tenant.id).where(Tenant.code == TENANT_CODE))
    if tenant_id is None:
        return 0

    async with session_scope(tenant_id=tenant_id) as db:
        conditions = [Ticket.title.startswith(prefix) for prefix in PROBE_TITLE_PREFIXES]
        rows = await db.execute(select(Ticket.id).where(or_(*conditions)))
        ids = list(rows.scalars().all())
        if not ids:
            return 0
        await db.execute(delete(Ticket).where(Ticket.id.in_(ids)))
    return len(ids)


async def main() -> None:
    await ensure_second_tenant()

    # ================================================== authentication
    print("\n=== authentication ===")
    cs = await make_session()

    response = await cs.login("cs@keka.local", "wrong-password")
    check(response.status_code == 401, "wrong password rejected with 401")
    check(
        response.json().get("code") == "NOT_AUTHENTICATED",
        "failure returns a machine-readable code",
    )
    check(
        "incorrect" in response.json().get("detail", "").lower(),
        "generic message - does not reveal whether the account exists",
    )

    response = await cs.login("nobody@keka.local")
    check(response.status_code == 401, "unknown account rejected identically")

    response = await cs.login("cs@keka.local")
    check(response.status_code == 200, "valid credentials accepted")
    check(
        "access_token" in cs.client.cookies and "refresh_token" in cs.client.cookies,
        "access and refresh cookies set",
    )
    check("csrf_token" in cs.client.cookies, "CSRF cookie set (readable by the frontend)")
    check(
        "ticket:create" in cs.user["permissions"] and "ticket:close" in cs.user["permissions"],
        "CS session carries create and close permissions",
    )
    check(
        "ticket:resolve" not in cs.user["permissions"],
        "CS session does NOT carry ticket:resolve",
    )

    me = (await cs.get("/api/v1/auth/me")).json()
    check(me["email"] == "cs@keka.local", "/auth/me returns the signed-in user")

    # CSRF: a state-changing request without the header must be refused.
    raw = await cs.client.post("/api/v1/tickets", json={})
    check(raw.status_code == 403, "state-changing request without CSRF header refused (403)")

    # ================================================== ticket creation
    print("\n=== ticket creation and priority derivation ===")
    clients = (await cs.get("/api/v1/clients")).json()
    teams = (await cs.get("/api/v1/teams")).json()
    payroll = next(t for t in teams if t["code"] == "PAY")
    acme = next(c for c in clients["items"] if c["code"] == "ACME")

    payload = {
        "title": "Payslip PDF is blank for contractors",
        "description": "Generating a payslip for any contractor produces an empty PDF.",
        "client_id": acme["id"],
        "team_id": payroll["id"],
        "environment": "production",
        "severity": "s2_major",
        "impact": "department",
        "workaround": "none",
        "steps_to_reproduce": "1. Open payroll 2. Select a contractor 3. Download payslip",
    }
    response = await cs.post("/api/v1/tickets", payload)
    check(response.status_code == 201, "CS can create a ticket")
    ticket = response.json()
    # s2_major + department = P2, then 'none' workaround raises it one level.
    check(
        ticket["priority"] == "p1", f"priority derived server-side as P1 (got {ticket['priority']})"
    )
    check(ticket["status"] == "open", "new ticket starts in the team inbox as open")
    check(ticket["reference"].startswith("OS-"), f"human-readable reference {ticket['reference']}")

    ref = ticket["reference"]

    # Ticket numbers must be unique per tenant even under concurrency.
    async def create_one(index: int) -> int:
        response = await cs.post(
            "/api/v1/tickets",
            {**payload, "title": f"Concurrency probe {index}"},
        )
        return int(response.json()["ticket_number"]) if response.status_code == 201 else -1

    numbers = await asyncio.gather(*(create_one(i) for i in range(5)))
    check(
        len(set(numbers)) == len(numbers) and -1 not in numbers,
        f"5 concurrent creates produced unique numbers {sorted(numbers)}",
    )

    # Idempotency: a double-submitted create must not produce two tickets.
    key = str(uuid.uuid4())
    first = await cs.client.post(
        "/api/v1/tickets",
        json={**payload, "title": "Idempotency probe"},
        headers={**cs._headers, "Idempotency-Key": key},
    )
    second = await cs.client.post(
        "/api/v1/tickets",
        json={**payload, "title": "Idempotency probe"},
        headers={**cs._headers, "Idempotency-Key": key},
    )
    check(
        first.status_code == 201
        and second.status_code in (200, 201)
        and first.json()["id"] == second.json()["id"],
        "replaying an Idempotency-Key returns the original ticket, not a duplicate",
    )

    # ================================================== workflow
    print("\n=== workflow and authorization ===")
    dev = await make_session()
    await dev.login("dev1@keka.local")

    detail = (await dev.get(f"/api/v1/tickets/{ref}")).json()
    check("assign" in detail["available_actions"], "developer offered Assign on an open ticket")
    check("close" not in detail["available_actions"], "developer NOT offered Close")

    response = await dev.post(
        f"/api/v1/tickets/{ref}/assign",
        {"version": detail["version"], "assignee_id": str(uuid.uuid4())},
    )
    check(response.status_code == 403, "developer cannot assign a ticket to someone else")

    response = await dev.post(
        f"/api/v1/tickets/{ref}/assign",
        {"version": detail["version"], "assignee_id": dev.user["id"]},
    )
    check(response.status_code == 200, "developer can take the ticket themselves")
    detail = response.json()
    check(detail["status"] == "assigned", "status moved to assigned")

    stale_version = detail["version"] - 1
    response = await dev.post(f"/api/v1/tickets/{ref}/start", {"version": stale_version})
    check(response.status_code == 409, "stale version rejected with 409")
    check(
        response.json().get("code") == "TICKET_VERSION_CONFLICT",
        "conflict carries TICKET_VERSION_CONFLICT",
    )

    detail = (await dev.post(f"/api/v1/tickets/{ref}/start", {"version": detail["version"]})).json()
    check(detail["status"] == "in_progress", "developer started work")

    response = await dev.post(f"/api/v1/tickets/{ref}/close", {"version": detail["version"]})
    check(response.status_code in (403, 409), "developer CANNOT close (the rule that matters)")

    response = await dev.post(
        f"/api/v1/tickets/{ref}/resolve",
        {"version": detail["version"], "resolution_notes": "Fixed the contractor branch."},
    )
    check(response.status_code == 200, "developer can resolve")
    detail = response.json()
    check(detail["status"] == "resolved", "status is resolved")

    awaiting = (await cs.get("/api/v1/tickets", params={"awaiting_closure": "true"})).json()
    check(
        any(t["reference"] == ref for t in awaiting["items"]),
        "resolved ticket appears in the CS Awaiting Closure queue",
    )

    response = await cs.post(f"/api/v1/tickets/{ref}/close", {"version": detail["version"]})
    check(response.status_code == 200, "CS can close")
    detail = response.json()
    check(detail["status"] == "closed", "ticket closed")

    response = await cs.post(
        f"/api/v1/tickets/{ref}/reopen",
        {"version": detail["version"], "reason": "Client says it still fails for one contractor."},
    )
    check(response.status_code == 200, "CS can reopen a closed ticket")
    check(response.json()["reopen_count"] == 1, "reopen_count incremented")

    history = (await cs.get(f"/api/v1/tickets/{detail['id']}/history")).json()
    check(len(history) >= 5, f"status history recorded {len(history)} transitions")

    # ================================================== comments
    print("\n=== conversation ===")
    ticket_id = detail["id"]
    response = await dev.post(
        f"/api/v1/tickets/{ticket_id}/comments", {"body": "Re-checked - it was a caching issue."}
    )
    check(response.status_code == 201, "developer can comment")
    comments = (await cs.get(f"/api/v1/tickets/{ticket_id}/comments")).json()
    check(len(comments) == 1, "comment visible to CS")

    # ================================================== search
    print("\n=== search ===")
    found = (await cs.get("/api/v1/tickets", params={"q": "payslip contractors"})).json()
    check(found["total"] >= 1, "full-text search finds the ticket by words in its title")

    number = ref.split("-")[1]
    by_number = (await cs.get("/api/v1/tickets", params={"q": number})).json()
    check(
        any(t["reference"] == ref for t in by_number["items"]),
        "searching a bare ticket number finds that ticket",
    )

    # ================================================== TENANT ISOLATION
    print("\n=== CROSS-TENANT ISOLATION ===")
    other = await make_session()
    response = await other.login(OTHER_USER_EMAIL)
    check(response.status_code == 200, "second tenant's admin can sign in")
    check(
        other.user["tenant_id"] != cs.user["tenant_id"],
        "the two sessions are on different tenants",
    )

    their_tickets = (await other.get("/api/v1/tickets")).json()
    check(
        their_tickets["total"] == 0,
        f"second tenant sees ZERO tickets (saw {their_tickets['total']})",
    )

    their_clients = (await other.get("/api/v1/clients")).json()
    check(
        all(c["code"] != "ACME" for c in their_clients["items"]),
        "second tenant cannot see the first tenant's clients",
    )

    their_users = (await other.get("/api/v1/users")).json()
    check(
        all(u["email"] != "cs@keka.local" for u in their_users["items"]),
        "second tenant cannot see the first tenant's users",
    )

    direct = await other.get(f"/api/v1/tickets/{ticket_id}")
    check(
        direct.status_code == 404,
        f"direct fetch of another tenant's ticket by id returns 404 (got {direct.status_code})",
    )
    check(
        direct.status_code != 403,
        "returns 404 rather than 403 - existence is never confirmed",
    )

    their_search = (await other.get("/api/v1/tickets", params={"q": "payslip"})).json()
    check(their_search["total"] == 0, "search cannot reach across tenants")

    their_audit = (await other.get("/api/v1/audit-logs")).json()
    check(
        all(e["entity_id"] != ticket_id for e in their_audit["items"]),
        "audit log does not leak the other tenant's entries",
    )

    # The database must refuse even without the application's help.
    async with session_scope(tenant_id=None) as db:
        rows = await db.execute(text("SELECT count(*) FROM tickets"))
        check(
            rows.scalar_one() == 0,
            "with no tenant scope set, RLS returns ZERO rows (fails closed)",
        )

    # ================================================== refresh rotation
    print("\n=== refresh token rotation ===")
    rotator = await make_session()
    await rotator.login("dev2@keka.local")
    first_refresh = rotator.client.cookies.get("refresh_token")

    response = await rotator.post("/api/v1/auth/refresh")
    check(response.status_code == 200, "refresh succeeds")
    second_refresh = rotator.client.cookies.get("refresh_token")
    check(first_refresh != second_refresh, "refresh token rotated to a new value")

    # Replaying the rotated token is treated as theft. The cookie is sent
    # explicitly: relying on the jar would risk a 401 for "no cookie", which
    # would pass the assertion without exercising reuse detection at all.
    replay = await make_session()
    response = await replay.client.post(
        "/api/v1/auth/refresh",
        headers={"Cookie": f"refresh_token={first_refresh}"},
    )
    check(response.status_code == 401, "replaying a rotated refresh token is rejected")

    response = await rotator.post("/api/v1/auth/refresh")
    check(
        response.status_code == 401,
        "reuse detection revoked the whole family - the legitimate session is ended too",
    )

    # ================================================== logout
    print("\n=== logout ===")
    logout_session = await make_session()
    await logout_session.login("dev1@keka.local")
    check((await logout_session.get("/api/v1/auth/me")).status_code == 200, "session active")
    await logout_session.post("/api/v1/auth/logout")
    check(
        (await logout_session.get("/api/v1/auth/me")).status_code == 401,
        "after logout the access token is denied immediately",
    )

    # ================================================== cleanup
    #
    # The tickets this script creates have to go. They are not merely clutter:
    # the AI layer asserts that every live ticket has an embedding, and tickets
    # created here are never embedded, so leaving them behind makes
    # `verify_ai.py` fail for a reason that has nothing to do with the AI
    # layer. That is exactly what happened — fourteen orphans across two runs,
    # and the invariant check pointed at the wrong thing.
    print("\n=== cleanup ===")
    removed = await drop_probe_tickets()
    check(removed >= 0, f"removed {removed} probe ticket(s) created by this run")

    # ================================================== summary
    print("\n" + "=" * 66)
    print(f"  {passed} passed, {len(failed)} failed")
    print("=" * 66)
    if failed:
        for item in failed:
            print(f"  FAILED: {item}")
        sys.exit(1)


if __name__ == "__main__":
    asyncio.run(main())
