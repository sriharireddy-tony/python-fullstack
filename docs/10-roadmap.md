# 10 — Development Roadmap

> **Status: all phases complete and verified against a live database.**
> See `backend/scripts/verify.py` and `backend/scripts/check_rls.py`.

Phases are **vertical slices**: each delivers working backend and frontend together, so there is
something usable at the end of every phase rather than a backend that waits months for a UI.

Each phase lists what is built and what "done" means. No phase starts before the previous one
works.

---

## Phase 0 — Foundation

**Goal:** both applications run locally and talk to each other. Nothing is a feature yet.

**Backend**
- Repository layout, `uv`, `ruff`, `mypy`, `pyproject.toml`
- FastAPI app factory, settings via `pydantic-settings`
- Async SQLAlchemy engine, session dependency, Alembic wired up
- Two database roles (`os_migrate`, `os_app`) and the RLS helper for tenant context
- Structured JSON logging with request-id middleware
- Redis client and key builder
- Problem Details error handler and the exception hierarchy
- `/health/live` and `/health/ready`

**Frontend**
- Vite + React + TypeScript, React Router, TanStack Query
- Bootstrap 5.3 with `data-bs-theme` wired to a theme context
- App shell: top nav, left nav, content area
- API client with credentials and CSRF header handling
- `openapi-typescript` generation as a build script

**Done when:** `/health/ready` reports the database and Redis reachable, the frontend renders the
shell, one generated type is imported and compiles, and a migration runs cleanly.

---

## Phase 1 — Identity, authentication, and RBAC

**Goal:** people can log in and see a navigation that matches their role.

**Backend**
- `tenants`, `platform_admins`, `users`, `user_teams`, `refresh_tokens` tables with RLS
- Argon2 hashing, JWT issue and verify, httpOnly cookies, CSRF tokens
- Refresh rotation with family reuse detection; Redis denylist
- Permission constants, role-to-permission map, `require(permission)` dependency
- `/auth/login`, `/refresh`, `/logout`, `/change-password`, `/me`
- Seed command: one tenant, one platform admin, one tenant admin

**Frontend**
- Login page, auth context, route guards
- Permission-driven left nav
- User menu: profile, change password, theme, density, logout
- `/profile` and preference persistence (server plus localStorage)

**Done when:** users of each role log in, see the correct nav, change theme and password, and a
deep link to an unauthorized route redirects rather than breaking.

> This phase carries the highest security risk in the project. It is worth being slow here.

---

## Phase 2 — Administration and reference data

**Goal:** the data tickets depend on can be managed.

**Backend**
- `teams`, `clients` tables, CRUD services and endpoints
- User management: create, edit, assign role and teams
- `POST /users/{id}/deactivate` with bulk ticket reassignment (the reassignment path is stubbed
  until Phase 3 and completed there)
- Redis caching for team and client lists, with delete-on-write invalidation
- Audit logging for every administrative change

**Frontend**
- Clients, Teams, Users list screens with the shared table component
- Create and edit modals with React Hook Form and Zod
- Deactivation flow with the reassignment prompt

**Done when:** an admin can create the six launch teams, several clients, and users in each role,
and every change appears in the audit log.

---

## Phase 3 — Ticket creation and listing

**Goal:** CS can raise a ticket and everyone can find it.

**Backend**
- `tickets` table with RLS, indexes, and the generated `search_vector`
- `tenant_counters` and race-safe ticket numbering (`SELECT ... FOR UPDATE`)
- Priority derivation as a pure function
- `POST /tickets` with `Idempotency-Key` support
- `GET /tickets` with full filtering, sorting, pagination, and full-text search
- `GET /tickets/{id}`, `PATCH /tickets/{id}` (content only, version-checked)
- `/meta/enums`

**Frontend**
- Create-ticket form: client, team, severity, impact, workaround, environment, repro fields
- Live preview of the computed priority as the user answers
- Ticket list with TanStack Table, server-side filters bound to URL search params
- Saved views in the left nav
- Ticket detail — read-only for now

**Done when:** CS creates a ticket, it gets `OS-1`, priority is computed correctly across the
matrix, and it is findable by number, text search, and every filter.

---

## Phase 4 — Ticket workflow

**Goal:** the ticket actually moves through its lifecycle.

**Backend**
- `ticket_status_history` table
- The transition table and resource policies in `tickets/policies.py`
- The nine action endpoints: assign, start, hold, resolve, reject, close, reopen, transfer-team,
  override-priority
- Optimistic locking enforced on every action
- Status history and audit entries written in the same transaction as the change
- Complete the bulk reassignment left stubbed in Phase 2

**Frontend**
- Contextual action buttons driven by permission and current state
- Action modals with their required fields (resolution notes, rejection reason, transfer reason,
  override justification)
- Version-conflict dialogue with reload
- History tab
- Team Inbox and Awaiting Closure views

**Done when:** a ticket can travel Open → Assigned → In Progress → Resolved → Closed and the
Rejected path, every illegal transition returns 409, only CS can close, and two users editing
simultaneously produce a clean conflict message rather than a silent overwrite.

---

## Phase 5 — Collaboration

**Goal:** people can discuss and evidence a ticket.

**Backend**
- `comments` and `attachments` tables
- The `FileStorage` interface with the local-disk implementation
- Upload validation: size, extension allowlist, content-type sniffing, opaque storage keys
- Permission-checked streaming download with `Content-Disposition: attachment` and `nosniff`

**Frontend**
- Conversation tab: comment list, composer, edit and soft delete for the author
- Attachments tab, drag-and-drop upload, image thumbnails
- Attachment upload from within a comment

**Done when:** a developer rejects a ticket with an explanation, CS replies in the conversation
and closes it, and screenshots upload and download correctly for permitted users only.

---

## Phase 6 — Dashboard and polish

**Goal:** the tool is pleasant enough that people choose to use it.

- Role-aware dashboard: my open tickets, team inbox count, awaiting closure count, recent activity
- **Team inbox count badge in the nav** — the substitute for notifications, and the mitigation for
  tickets sitting unnoticed
- Global search in the top nav, jumping straight to a ticket by number
- Empty, loading, and error states audited across every screen
- Compact density applied consistently to tables
- Keyboard shortcuts for the ticket list

**Done when:** a developer opening the app immediately sees what needs their attention without
applying a single filter.

---

## Phase 7 — Hardening

**Goal:** ready for real users.

- Rate limiting on auth endpoints, with the in-memory fallback
- Redis failure behaviour verified for each use case
- Audit log UI with filtering
- Log-redaction review — confirm no ticket or comment body reaches the logs
- Security headers and CORS locked down
- A cross-tenant isolation test with two seeded tenants
- The CI check for `tenant_id` and RLS on every table
- Seed and demo data for a pilot

**Done when:** a second seeded tenant is provably invisible to the first through every endpoint,
filter, search, and cache path.

---

## Then: pilot

**Recommendation: one team for two to three weeks before opening it up.** Payroll or CoreHR.

> One real team using it for a fortnight surfaces more about what the form is missing than
> another month of design discussion. It also keeps the blast radius of a wrong assumption
> small.

---

## Deferred — designed for, not built

Each of these is a contained addition because the groundwork exists.

| Feature | What already supports it |
|---|---|
| **Notifications** (email, Teams) | Redis is present; the events are already logged |
| **SLA and escalation** | `ticket_status_history` records every transition with timestamps |
| **Reporting and MTTR** | Same history table — data exists from Phase 4 onward |
| **Duplicate detection** | `duplicate_of_id` column exists, unused |
| **ADO synchronisation** | The link field exists; status is a clean state machine an integration can drive |
| **Product module dimension** | Teams are a lookup table, so adding modules is additive |
| **Customer-facing portal** | Tenant isolation and client records are already modelled |
| **AI automation** | Service layer is HTTP-free, so agents call the same code paths |

## Parked — needs a decision

| Item | Blocks |
|---|---|
| **Hosting and deployment** | Everything below |
| **CI/CD** | Automated deployment |
| **Monitoring and alerting** | Production readiness |
| **Automated testing** | Confidence in the state machine and RBAC. See the risk in [doc 11](11-decisions-and-risks.md) |
| **Data retention policy** | Compliance, and the purge job |
| **Backup and restore** | Production readiness |
