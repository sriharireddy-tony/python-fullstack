# 11 — Decisions & Risks

A record of what was decided, why, what was rejected, and what would change the answer. When a
decision is revisited, amend the entry rather than deleting it — the reasoning is the valuable
part.

---

## Decision log

### D-01 — Multi-tenant, shared database and shared schema

**Decided.** One database, `tenant_id` on every tenant-scoped table, row-level security as the
isolation guarantee. One tenant at launch.

**Why:** lowest operational cost, a single migration path, and straightforward cross-tenant
operational reporting.

**Alternatives:** schema per tenant (better isolation, but migrations across N schemas become a
chore); database per tenant (best isolation, highest cost).

**Revisit when:** a tenant has a contractual data-residency or physical-isolation requirement.
That tenant moves to its own database; the shared design does not block it.

---

### D-02 — Modular monolith, not microservices

**Decided.** One FastAPI application, internally modular, with services as the only cross-module
interface.

**Why:** every ticket action touches status, history, audit, and cache together — one transaction
in a monolith, a distributed-consistency problem otherwise. The scale does not call for anything
more.

**Alternatives:** microservices (all the operational cost, none of the benefits at this size);
an unstructured monolith (faster for a month, then boundaries exist only on a diagram).

**Revisit when:** a module develops a genuinely different scaling profile or release cadence.

---

### D-03 — "Customer" is data, "tenant" is an organisation using OS Tracker

**Decided.** Two distinct levels: `tenants` (organisations using the tool) and `clients` (the
customer companies whose issues are reported). Clients never authenticate.

**Why:** without a client record, questions like "which customers are affected by this bug?" and
"show everything open for Acme before their renewal" cannot be answered — and that data cannot be
backfilled.

**Revisit when:** a customer-facing portal is wanted. The model already supports it.

---

### D-04 — Priority is derived, never entered

**Decided.** CS answers severity, impact, and workaround. The system computes P1–P4. Overrides
are allowed with a mandatory recorded reason.

**Why:** when the person raising a ticket also sets its priority, everything becomes P1 within a
few months. Not bad faith — every agent believes their customer's problem is urgent, and nothing
costs them for saying so.

**Alternatives:** direct priority selection (simpler form, loses all meaning); a fully automated
model with no override (no escape hatch when the matrix is wrong).

**Revisit when:** override frequency shows the matrix is systematically wrong. Tune the matrix,
not the process.

---

### D-05 — SLA deferred; priority is the only urgency signal

**Decided** at the user's direction.

**Consequence:** no deadlines, no breach views, no aging alerts, no escalation.

**Hedge taken:** `ticket_status_history` records every transition with a timestamp from Phase 4,
so SLA, MTTR, and aging reports remain possible later — **including for tickets created before
the feature exists**. This data cannot be reconstructed if not captured.

**Revisit when:** customers begin complaining about response time, or leadership asks for MTTR.

---

### D-06 — Notifications deferred

**Decided** at the user's direction.

**Consequence:** nobody is told a ticket arrived. Developers must open the app and check.

**Mitigation:** a count badge on the team inbox, so an unattended queue is visible immediately on
opening the app.

**Revisit when:** tickets are observed sitting unnoticed. Redis is already present, so this
becomes a feature rather than an infrastructure project.

---

### D-07 — Engineers resolve; only CS closes

**Decided.**

**Why:** the developer knows the code is fixed. Only CS knows the *customer agrees* it is fixed.
If developers could close, the system would report 100% closure while customers were still
complaining, and nobody would notice for months.

**Consequence:** `resolved` and `rejected` are the same kind of state — engineering is done, CS
must act. Both land in the CS "Awaiting Closure" queue.

**Revisit when:** never, ideally. This is the constraint that keeps the tool honest.

---

### D-08 — Visibility is view scoping, not access control

**Decided.** Own tickets by default, team inbox for unassigned, all tickets readable but not
editable.

**Why the stricter alternative was rejected:** restricting reads to the assignee would break
search and duplicate-finding, prevent colleagues helping each other, and — combined with user
deactivation — make a departed engineer's open tickets invisible to everyone.

**Revisit when:** a genuine confidentiality requirement appears, such as certain clients' tickets
needing restriction. That would need ticket-level access control, which is a different design.

---

### D-09 — JWT in httpOnly cookies

**Decided.** JWT is the format; the cookie is the transport. Both, not either.

**Why:** the application renders user-supplied ticket text, which is where XSS comes from. A token
in localStorage means one missed escape leaks every session.

**Trade-off:** CSRF protection is required — SameSite plus a double-submit token.

**Revisit when:** a mobile app or third-party API client exists. Add bearer tokens alongside;
do not replace.

---

### D-10 — Role in the token, permissions resolved server-side

**Decided.**

**Why:** embedding permissions makes every issued token stale the moment a role's permissions
change. Resolving from a single authoritative map means changes take effect immediately, at the
cost of a dictionary lookup.

---

### D-11 — Actions are endpoints, not a status field

**Decided.** `POST /tickets/{id}/close`, not `PATCH` with a status.

**Why:** each action has a different permission, different required fields, and different side
effects. A generic status PATCH collapses all of that into one branching function and makes the
permission matrix impossible to express at the route level.

---

### D-12 — Offset pagination, not keyset

**Decided.**

**Why:** the UI needs page numbers and a total count. Keyset provides neither. Offset only
degrades on very large tables.

**Revisit when:** a single tenant passes a few hundred thousand tickets, or deep pages become
slow.

---

### D-13 — Modules with layers inside, not top-level layer folders

**Decided.** `modules/tickets/{models,schemas,repository,service,policies,router}.py` rather than
top-level `models/`, `schemas/`, `repositories/`.

**Why:** layer-first works for small apps, but every change touches five folders and — more
importantly — when every model sits in one directory, nothing discourages one module's service
from importing another module's models. That is exactly the coupling a modular monolith exists to
prevent.

---

### D-14 — Redis in v1, scoped

**Decided.** Rate limiting, idempotency keys, reference-data caching, and the token denylist.
**Not** ticket data.

**Why not tickets:** they change constantly and are filtered many ways, and each query is already
fast and indexed. The invalidation effort would exceed the saving, and stale ticket state erodes
trust in the tool.

---

### D-15 — Local file storage now, Azure Blob later, behind an interface

**Decided.**

**Consequence:** the application server is stateful. Two instances need a shared volume, and a
restart without a mounted volume loses every screenshot.

**Why acceptable:** a single instance at launch, and the constraint disappears entirely on the
move to Azure Blob — which is a config change, because business logic never sees a path.

---

### D-16 — Bootstrap plus TanStack Table

**Decided.** Bootstrap 5.3 via react-bootstrap, with TanStack Table for data grids.

**Why the pairing:** Bootstrap provides CSS, not a data grid — and this application is
fundamentally a large filterable table.

**Revisit when:** the team finds itself also hand-building a date-range picker, multi-select, and
toast manager. That is the signal MUI, Ant Design, or Mantine would have paid off — and switching
before the ticket list is built is far cheaper than at 60% done.

---

### D-17 — Frontend types generated from the backend OpenAPI schema

**Decided.** `openapi-typescript` as a build step; generated files are never hand-edited.

**Why:** it is the single biggest advantage of this stack combination. Change a Pydantic model,
regenerate, and the frontend fails to compile everywhere that is now wrong. The alternative is
hand-maintaining two copies of every model and finding drift in production.

---

### D-18 — Login resolves the tenant through a SECURITY DEFINER function

**Decided.** ``auth_tenant_for_email(text) RETURNS uuid`` (migration 0003),
owned by the migration role, callable only by the application role.

**Why:** ``users`` is behind FORCE row-level security, so a query with no
``app.tenant_id`` returns nothing — which is exactly right, and exactly the
problem at login, where the tenant is not known until the user is found. The
function answers one question and returns one uuid: no password hash, no name,
no other column. The caller then sets the tenant scope and loads the user
through ordinary RLS.

**Alternatives rejected:** granting BYPASSRLS to the *application* role (kills
isolation everywhere, for one query); a policy allowing reads when the scope is
unset (inverts the fail-closed default the whole design rests on); a separate
unscoped email→tenant table (a second copy of the truth, which will drift).

**Consequence:** the migration role carries BYPASSRLS, because a SECURITY
DEFINER function runs as its owner and FORCE applies to owners too. Acceptable —
that role already owns the schema and can drop any table, and the application
never connects as it. `scripts/check_rls.py` asserts the *application* role does
not have it.

---

### D-19 — Enum columns declare `values_callable`

**Decided.** All Postgres enum columns are built by ``pg_enum()`` in
``app/core/models.py``.

**Why:** SQLAlchemy persists a Python enum by *member name* (``ENTERPRISE``),
not its value (``enterprise``). The database types are declared with lowercase
values, so without this every insert fails with "invalid input value for enum".
Centralising it means a new enum column cannot get this wrong.

---

### D-20 — `updated_at` uses a Python-side `onupdate`

**Decided.** ``onupdate=_utcnow`` rather than ``onupdate=func.now()``.

**Why:** a SQL-side onupdate leaves the attribute expired after every UPDATE, so
the next read triggers an implicit refresh — which async SQLAlchemy forbids,
raising ``MissingGreenlet`` from whatever happened to touch it, usually response
serialisation. A Python value is set during flush and is immediately readable.

**Trade-off:** the timestamp comes from the application clock rather than the
database's. For a field measured in seconds, acceptable. ``created_at`` keeps its
server default, which is fetched via RETURNING on INSERT and so is unaffected.

---

### D-21 — Reuse detection commits before raising

**Decided.** ``AuthService.refresh`` commits the family revocation before
raising ``AuthenticationError``.

**Why:** this is the one place a service commits, and it is deliberate. Raising
unwinds to the session dependency, which rolls the transaction back — silently
undoing the very revocation the branch exists to perform. Found by integration
testing: the replayed token was rejected, but the stolen family stayed live.

**How to spot the same class of bug elsewhere:** any security action taken on a
path that then raises.

---

## Accepted risks

Consequences of decisions already made. All defensible for v1; none should be a surprise later.

| # | Risk | Consequence | Mitigation | Revisit when |
|---|---|---|---|---|
| R-1 | **No automated unit tests** | Nothing runs on every change; a regression is caught only when someone remembers to run the verification scripts | `scripts/verify.py` (54 end-to-end checks) and `scripts/check_rls.py` exist and pass, plus standalone checks for the priority matrix, state machine, JWT handling, and log redaction | Before real users. The scripts are most of the work already; wiring them into pytest and CI is the remaining step |
| R-2 | **No notifications** | Nobody knows a ticket arrived | Team inbox count badge | Tickets observed sitting unnoticed |
| R-3 | **No SLA** | No deadlines or aging signal | Priority, plus history captured for later | Response-time complaints |
| R-4 | **Local file storage** | Stateful app server; restart without a volume loses uploads | Single instance, mounted volume | Moving to Azure Blob, or scaling out |
| R-5 | **No virus scanning on uploads** | A malicious file could be stored and re-downloaded | Internal users only; `Content-Disposition: attachment` and `nosniff` prevent in-origin execution | Any external upload path |
| R-6 | **No duplicate detection** | One bug arrives four times; four developers fix it | Full-text search so CS can check first | Ticket volume grows |
| R-7 | **No MFA** | Password-only authentication | Rate limiting, Argon2, short sessions | Internet exposure, or a second tenant |
| R-8 | **No monitoring** | Failures are discovered by users | Structured logs are ready for any collector | Deployment is decided |
| R-11 | **Rate limiting and logout revocation degrade to per-instance without Redis** | On several instances, a logged-out access token stays usable elsewhere for up to 15 minutes, and login limits are counted per instance | In-process fallback keeps both correct on a single instance; the damage window is bounded by the short access-token TTL | Running more than one application instance |
| R-9 | **No data retention policy** | Payroll screenshots accumulate indefinitely | Soft delete and timestamps make a purge job additive | Before significant data accumulates — this is a policy decision, not a technical one |
| R-10 | **No backup or restore plan** | Data loss would be unrecoverable | — | **Before the pilot.** This one should not wait for the full deployment decision |

> R-10 is the one worth raising early. Everything else on this list degrades the product; that one
> loses it.

---

## Parked — needs a decision before the relevant phase

| Item | Needed by |
|---|---|
| Hosting and deployment target | Phase 7 / pilot |
| CI/CD pipeline | First deployment |
| Monitoring, alerting, error tracking | Pilot |
| Automated testing scope | Before real users |
| Backup and restore | Before the pilot |
| Data retention policy | Before significant data accumulates |
| Encryption at rest | With the hosting decision |
| SSO (Microsoft Entra ID) | When Azure access exists; the auth provider interface is already designed for it |
