# OS Tracker

Operational Support Tracker — an internal, multi-tenant application connecting the customer
support team to engineering teams. Support raises issues reported by customers; engineering
teams pick them up, work them, and resolve them; support verifies and closes.

**The architecture is documented before the code.** Read [`docs/`](docs/README.md) first —
particularly [`docs/11-decisions-and-risks.md`](docs/11-decisions-and-risks.md), which records
why each decision was made and what would change it.

---

## Status

**All phases complete and verified against a live database.** See
[`docs/10-roadmap.md`](docs/10-roadmap.md) for what each phase covered.

| Phase | Scope | Status |
|---|---|---|
| 0 | Foundation, logging, errors, health, app shell | Done |
| 1 | Identity, authentication, RBAC | Done |
| 2 | Administration and reference data | Done |
| 3 | Ticket creation and listing | Done |
| 4 | Ticket workflow | Done |
| 5 | Collaboration (comments, attachments) | Done |
| 6 | Dashboard, saved views, nav badges | Done |
| 7 | Hardening: rate limiting, idempotency, RLS gate | Done |

### Verification

```bash
cd backend
.venv\Scripts\python -m scripts.check_rls    # tenant isolation gate
.venv\Scripts\python -m scripts.verify       # 54 end-to-end checks
```

`scripts/verify.py` runs against a real database and covers authentication,
CSRF, priority derivation, concurrent ticket numbering, the full workflow,
optimistic locking, search, refresh-token rotation with reuse detection, logout
revocation, rate limiting, idempotency, and **ten cross-tenant isolation
checks**.

Automated unit tests remain deliberately deferred (risk **R-1**). The
verification suite covers the behaviour that matters most; it is not a
substitute for a test suite that runs on every change.

---

## Stack

| | |
|---|---|
| Backend | FastAPI · SQLAlchemy 2.0 (async) · Alembic · PostgreSQL · Redis |
| Frontend | React 18 + TypeScript · Vite · Bootstrap 5.3 · TanStack Query & Table |
| Auth | JWT in httpOnly cookies (Phase 1) |
| Tenancy | Shared database, shared schema, PostgreSQL row-level security |

---

## Prerequisites

| Requirement | Notes |
|---|---|
| Python 3.12+ | Verified on 3.14 |
| Node 20+ | Verified on 24 |
| PostgreSQL 14+ | Required |
| Redis | **Optional in development** — caches fail open and fall through to Postgres |

---

## First-time setup

### 1. Database

Run once as a Postgres superuser. Creates two roles and the database.

```bash
# psql is not on PATH by default on Windows
& "C:/Program Files/PostgreSQL/18/bin/psql.exe" -U postgres -f backend/scripts/bootstrap_database.sql
```

Then apply the schema and seed development data:

```bash
cd backend
.venv\Scripts\python -m alembic upgrade head
.venv\Scripts\python -m scripts.seed          # prints the sign-in credentials
```

> **Two roles is not optional.** `os_migrate` owns the schema, `os_app` owns nothing. Row-level
> security is bypassed by the table owner — if the application connected as the migration role,
> every tenant-isolation policy would be decorative. See
> [`docs/04-database.md`](docs/04-database.md).

Change both passwords in the script before running it outside local development.

### 2. Backend

```bash
cd backend
py -m venv .venv                       # Windows;  python3 -m venv .venv elsewhere
.venv\Scripts\python -m pip install -e ".[dev]"
copy .env.example .env                 # then edit the passwords to match step 1
```

### 3. Frontend

```bash
cd frontend
npm install
```

---

## Running

Two terminals:

```bash
# backend  ->  http://localhost:8000
cd backend
.venv\Scripts\python -m uvicorn app.main:app --reload

# frontend ->  http://localhost:5173
cd frontend
npm run dev
```

Open **http://localhost:5173**. The Vite dev server proxies `/api` and `/health` to the
backend, so the browser sees a single origin — which keeps the auth cookies first-party in
development, exactly as they behave in production.

| URL | |
|---|---|
| http://localhost:5173 | Application |
| http://localhost:8000/docs | API documentation (disabled in production) |
| http://localhost:8000/health/ready | Dependency status |

---

## Quality gates

Everything below must pass before a change is considered done.

```bash
# backend
cd backend
.venv\Scripts\ruff check app alembic        # lint
.venv\Scripts\ruff format --check app alembic
.venv\Scripts\mypy app                      # strict type checking

# frontend
cd frontend
npm run typecheck
npm run build
```

Automated tests are deliberately deferred — see risk **R-1** in
[`docs/11-decisions-and-risks.md`](docs/11-decisions-and-risks.md).

---

## Layout

```
docs/                  architecture, decided before implementation
backend/
  app/
    core/              config, database, logging, errors, cache, middleware
    modules/           one folder per domain module
    api/v1/            routers assembled from the modules
  alembic/             migrations
  scripts/             database bootstrap
frontend/
  src/
    api/               client, query hooks, generated types
    features/          mirrors the backend modules
    layout/            app shell, navigation
    components/        shared UI
    lib/               permissions, theme, session, navigation
```

**The rule that keeps this a modular monolith:** modules communicate through **services**,
never by importing another module's models or querying its tables directly.

---

## Generating frontend types

The frontend's TypeScript types come from the backend's OpenAPI schema. With the backend
running:

```bash
cd frontend
npm run api:types
```

Change a Pydantic model, regenerate, and the frontend **fails to compile** everywhere that is
now wrong. That compile error is the point — it is what stops the two sides drifting apart.
