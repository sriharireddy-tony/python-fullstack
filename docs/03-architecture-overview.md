# 03 — Architecture Overview

## System context

```
   Browser (Chrome / Edge, desktop)
        |
        |  HTTPS, httpOnly cookies
        v
   React + TypeScript SPA  (Vite build, static assets)
        |
        |  REST /api/v1, JSON
        v
   FastAPI application  ------------------+
        |                                 |
        |                                 +---> Redis
        |                                 |     rate limits, idempotency keys,
        |                                 |     reference-data cache, token denylist
        v                                 |
   PostgreSQL                             +---> File storage
   single database, shared schema,              local disk now, Azure Blob later,
   row-level security per tenant                behind one interface
```

There are no external integrations in v1. HubSpot is out of scope, and the Azure DevOps link
is a free-text field, not a connection.

---

## Modular monolith

**Decided.** One deployable FastAPI application, internally divided into modules with enforced
boundaries.

### Why, for this system

- One deployment, one database, one set of logs. Operating this is a part-time job, not a
  full-time one.
- Every ticket action touches several concerns at once — status, history, audit, cache
  invalidation. In a monolith that is one database transaction. Across services it is a
  distributed-consistency problem, and the failure modes are far harder to reason about.
- The scale simply does not call for anything else. Microservices solve independent scaling
  and independent release cadence. Neither is a problem here.

### Alternatives considered

| Option | Why not |
|---|---|
| **Microservices** | All the operational cost, none of the benefits at this size. Would turn a single transaction into a saga |
| **Unstructured monolith** | Faster for the first month, then every module imports every other and the boundaries exist only on a diagram |
| **Serverless functions** | Cold starts, awkward connection pooling against Postgres, and RLS session variables become fragile |

### When this should be revisited

If one module develops a genuinely different scaling profile or release cadence — a
high-volume ingestion path, or an AI workload with different hardware needs. The module
boundaries are what make that extraction possible later.

### The rule that makes it modular

> **Modules communicate through services, never by importing another module's models or
> querying its tables directly.**

Enforce it in code review. If a module is ever extracted into its own service, this boundary is
what makes it feasible; if it never is, nothing has been lost.

---

## Module map

| Module | Owns |
|---|---|
| `tenancy` | Tenants, platform super admins, per-tenant counters |
| `identity` | Users, roles, permissions, authentication, refresh tokens |
| `teams` | Teams and team membership |
| `clients` | Customer accounts referenced by tickets |
| `tickets` | Ticket entity, state machine, priority derivation, status history, policies |
| `comments` | Ticket conversation |
| `attachments` | File metadata and the storage abstraction |
| `audit` | Append-only change log |

Dependencies flow one way. `tickets` calls into `clients`, `teams`, and `identity` through
their services; none of those know `tickets` exists.

---

## Technology stack

### Backend

| Concern | Choice | Why | Alternative, and when to switch |
|---|---|---|---|
| Framework | **FastAPI** | Async-native, Pydantic validation at the boundary, and an OpenAPI schema we use to generate frontend types | Django REST is more batteries-included but heavier; choose it only if the Django admin is wanted |
| Language | **Python 3.12+** | Modern typing, real performance gains | — |
| ORM | **SQLAlchemy 2.0**, typed | Mature, and it does not hit a ceiling on RLS, recursive queries, or complex filters | SQLModel is tempting but thinner; its limits appear on exactly the queries this app needs |
| Migrations | **Alembic** | Standard, autogenerate works well with SQLAlchemy 2.0 | — |
| Driver | **asyncpg**, async sessions throughout | See the note below | psycopg with sync sessions |
| Validation | **Pydantic v2** | Same models drive validation and the OpenAPI schema | — |
| Passwords | **Argon2** | Current best practice | bcrypt is acceptable |
| Cache | **Redis** | Shared counters and TTL semantics. See [doc 09](09-logging-and-caching.md) | — |
| Dependencies | **uv** | Much faster than pip or Poetry, lockfile included | Poetry, if the team already uses it |
| Quality | **ruff** (lint and format) + **mypy** | One tool for lint and format; mypy pays for itself in a typed codebase | — |

> **Async end to end — the one real trap.** FastAPI is async. Using a *sync* ORM session inside
> an `async def` route blocks the event loop, and throughput collapses under concurrency. It is
> a subtle bug that only appears under load. Going async throughout removes the whole class of
> problem. The cost is harder stack traces and the discipline of never calling blocking I/O in
> a route.

**No background job runner in v1.** There are no notifications, no SLA timers, and no scheduled
work — a broker and worker would be infrastructure operated for nothing. When jobs are needed,
**ARQ** (async, Redis-backed, minimal) fits this stack better than Celery, and Redis is already
present.

### Frontend

| Concern | Choice | Why | Alternative, and when to switch |
|---|---|---|---|
| Build | **Vite** | Fast, simple SPA build | Next.js adds SSR and SEO that an internal tool behind a login does not need |
| Language | **React 18+ with TypeScript** | Types generated from the backend schema make the boundary safe | — |
| Routing | **React Router v7** | Standard | — |
| Server state | **TanStack Query** | The highest-value pick here: caching, refetching, loading and error states all solved | — |
| Client state | React Context, or Zustand if it grows | Almost all state in this app is server state | **Not Redux** — with TanStack Query there is very little left for it to do |
| Forms | **React Hook Form + Zod** | Real validation on ticket forms; Zod schemas mirror the Pydantic models | Formik works but is slower and less type-friendly |
| UI | **Bootstrap 5.3 via react-bootstrap** | Chosen. React components rather than jQuery-era JS, and native dark mode via `data-bs-theme` | See note below |
| Tables | **TanStack Table**, styled with Bootstrap | Bootstrap has no data grid, and this app is mostly filterable tables | — |
| API types | **openapi-typescript** generated from FastAPI | See note below | Hand-written types that silently drift |

> **On Bootstrap:** it provides CSS, not a data grid, date-range picker, or toast system.
> Pairing it with TanStack Table covers the largest gap without abandoning the chosen design
> system. *Reconsider* if the team ends up hand-building a date picker, multi-select, and modal
> manager as well — that is the signal MUI, Ant Design, or Mantine would have paid off, and it
> is far cheaper to switch before the ticket list is built than at 60% done.

> **Generating frontend types from the backend is the single biggest reason this stack
> combination works well.** FastAPI publishes an OpenAPI schema; `openapi-typescript` turns it
> into TypeScript types as a build step. Change a Pydantic model, regenerate, and the frontend
> fails to compile everywhere that is now wrong. Skipping it means hand-maintaining two copies
> of every model and discovering drift in production.

---

## Repository layout

```
os-tracker/
├─ docs/                      this documentation
├─ backend/
│  ├─ app/
│  │  ├─ core/                config, database, security, dependencies, logging
│  │  ├─ modules/
│  │  │  ├─ tenancy/
│  │  │  ├─ identity/
│  │  │  ├─ teams/
│  │  │  ├─ clients/
│  │  │  ├─ tickets/
│  │  │  ├─ comments/
│  │  │  ├─ attachments/
│  │  │  └─ audit/
│  │  ├─ api/v1/              routers assembled from the modules
│  │  └─ main.py
│  ├─ alembic/
│  ├─ tests/
│  └─ pyproject.toml
└─ frontend/
   ├─ src/
   │  ├─ api/                 generated types, client, query hooks
   │  ├─ features/            tickets, clients, teams, users, auth, profile
   │  ├─ components/          shared UI
   │  ├─ layout/              AppShell, TopNav, SideNav, UserMenu
   │  ├─ lib/                 permissions, formatting, constants
   │  └─ routes/
   └─ package.json
```

Each backend module contains the same five files: `models.py`, `schemas.py`, `repository.py`,
`service.py`, `router.py`, plus `policies.py` where resource rules exist.

> **Why modules-with-layers rather than top-level `models/`, `schemas/`, `repositories/`:**
> the layer-first layout is common and works for small apps, but it means every change touches
> five folders, and — more importantly — when every model sits in one directory nothing
> discourages `services/ticket.py` from importing `models/user.py` directly. That is exactly
> the coupling a modular monolith exists to prevent. Nesting the same layers inside modules
> keeps the layering while making boundaries visible in the directory tree.

Frontend feature folders mirror backend modules deliberately: a change to ticket rejection
touches `backend/app/modules/tickets/` and `frontend/src/features/tickets/`, and nothing else.
