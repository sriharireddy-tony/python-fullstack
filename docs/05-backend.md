# 05 — Backend

FastAPI, Python 3.12+, SQLAlchemy 2.0 (async), PostgreSQL, Redis. One deployable application,
internally modular.

## Structure

```
backend/
├─ app/
│  ├─ core/
│  │  ├─ config.py           settings from environment (pydantic-settings)
│  │  ├─ database.py         engine, session factory, tenant context
│  │  ├─ security.py         hashing, JWT encode/decode, CSRF
│  │  ├─ dependencies.py     get_db, get_current_user, require(permission)
│  │  ├─ logging.py          structured JSON logging, request-id context
│  │  ├─ cache.py            Redis client, key builder
│  │  ├─ errors.py           exception types, Problem Details handler
│  │  └─ pagination.py       shared page params and response envelope
│  ├─ modules/
│  │  ├─ tenancy/
│  │  ├─ identity/
│  │  ├─ teams/
│  │  ├─ clients/
│  │  ├─ tickets/
│  │  ├─ comments/
│  │  ├─ attachments/
│  │  └─ audit/
│  ├─ api/
│  │  └─ v1/router.py        assembles module routers under /api/v1
│  └─ main.py                app factory, middleware, lifespan
├─ alembic/
├─ tests/
└─ pyproject.toml
```

### Every module has the same shape

```
modules/tickets/
├─ models.py        SQLAlchemy ORM models
├─ schemas.py       Pydantic request and response models
├─ repository.py    data access only — queries, no business rules
├─ service.py       business rules, orchestration, transactions
├─ policies.py      resource-level authorization (layer 3)
├─ router.py        HTTP layer — parse, delegate, serialise
└─ exceptions.py    module-specific errors
```

---

## Layer responsibilities

| Layer | Does | Must not |
|---|---|---|
| **Router** | Parse and validate input, declare permissions, call one service method, serialise the result | Contain business rules, touch the database, or make authorization decisions beyond the permission dependency |
| **Service** | Enforce business rules and resource policy, orchestrate repositories, own the transaction, write audit and history entries, invalidate cache | Know anything about HTTP — no `Request`, no `HTTPException` |
| **Repository** | Build and execute queries, map rows to models | Contain business rules or decide what is allowed |
| **Policy** | Answer "may this user do this to this object in this state?" | Perform I/O |

> **Why the service layer must be HTTP-free:** it is the reusable core. A background job, a CLI
> command, or a future automation calls the same service and gets the same rules, audit
> entries, and validation. If business logic lives in route handlers, every other caller has to
> make HTTP requests to the application's own API to get correct behaviour.

---

## Request lifecycle

```
1. Request ID middleware        generate or read X-Request-ID, bind to contextvar
2. Logging middleware           start timer, bind request metadata
3. CORS / security headers
4. Route dependency: get_current_user
       read the access-token cookie, verify the JWT signature and expiry,
       check the jti against the Redis denylist, load the user
5. Route dependency: require(Permission.X)      <-- authorization layer 2
6. Route dependency: get_db
       open a transaction
       SET LOCAL app.tenant_id = <tenant from the token>   <-- layer 1
7. Router handler -> Service
       policy check                                        <-- layer 3
       repository calls
       audit + status-history writes
       cache invalidation
8. Commit. Serialise the response
9. Logging middleware           emit one structured line: status, duration, ids
```

**Tenant context is set once, from the token, inside the transaction.** It is never read from a
path parameter, a query string, or a request body — a request cannot ask to act on another
tenant.

---

## Configuration

`pydantic-settings`, environment variables only, no secrets in the repository. `.env.example`
is committed; `.env` is not.

| Variable | Purpose |
|---|---|
| `DATABASE_URL` | Postgres DSN for the `os_app` role |
| `MIGRATION_DATABASE_URL` | Postgres DSN for the `os_migrate` role |
| `REDIS_URL` | Cache and rate limiting |
| `JWT_SECRET` | Access-token signing key |
| `ACCESS_TOKEN_TTL_MINUTES` | Default 15 |
| `REFRESH_TOKEN_TTL_DAYS` | Default 7 |
| `STORAGE_BACKEND` | `local` or `azure_blob` |
| `STORAGE_LOCAL_PATH` | Upload directory when local |
| `CORS_ORIGINS` | Allowed frontend origins |
| `LOG_LEVEL` | Default `INFO` |
| `ENVIRONMENT` | `local`, `staging`, `production` |

The application **fails to start** if a required variable is missing. A misconfigured app that
starts and misbehaves is worse than one that refuses to boot.

---

## Error handling

One exception hierarchy, one handler, one response shape.

| Exception | HTTP | `code` |
|---|---|---|
| `NotFoundError` | 404 | `RESOURCE_NOT_FOUND` |
| `PermissionDeniedError` | 403 | `PERMISSION_DENIED` |
| `ValidationError` | 422 | `VALIDATION_FAILED` |
| `ConflictError` | 409 | `TICKET_VERSION_CONFLICT`, `INVALID_TRANSITION`, ... |
| `RateLimitError` | 429 | `RATE_LIMITED` |
| Unhandled | 500 | `INTERNAL_ERROR` |

Services raise domain exceptions. A single handler converts them into RFC 9457 Problem Details
(see [doc 06](06-api.md)). Route handlers never construct `HTTPException` themselves.

**Cross-tenant access returns 404, not 403** — the API must never confirm that a resource exists
in another tenant.

Unhandled exceptions log a full stack trace with the request ID, and return a generic message
with that ID. Internal details never reach the client.

---

## Key patterns

### Storage abstraction

```python
class FileStorage(Protocol):
    async def save(self, key: str, data: BinaryIO, content_type: str) -> None: ...
    async def open(self, key: str) -> BinaryIO: ...
    async def delete(self, key: str) -> None: ...
    async def signed_url(self, key: str, ttl: int) -> str | None: ...
```

`LocalFileStorage` now, `AzureBlobStorage` later, selected by `STORAGE_BACKEND`. Business logic
never sees a path.

> Roughly thirty lines of abstraction that turn a miserable week of find-and-replace into a
> config change. `signed_url` returns `None` for local storage, where downloads stream through
> the application instead.

**Local storage makes the app server stateful:** two instances behind a load balancer need a
shared volume, and a container restart without a mounted volume loses every screenshot. Fine
for a single instance; the constraint disappears on Azure Blob.

### Priority derivation

A pure function in `tickets/service.py` — no I/O, no database. Takes severity, impact, and
workaround, returns a priority. Deterministic and trivially testable.

### State machine

A single transition table in `tickets/policies.py` mapping `(from_status, to_status)` to the
required permission and resource rule. Every action endpoint consults it. Nothing about
transitions lives in route handlers.

### Audit and history

Written by the service, inside the same transaction as the change itself.

> If the audit write is a separate transaction or a background task, a crash between the two
> leaves a change with no record — precisely the case where the record matters most.

### Cache invalidation

Delete-on-write, performed by the service. See [doc 09](09-logging-and-caching.md).

---

## Database session and connection pooling

- One `AsyncSession` per request, provided by dependency injection.
- The transaction wraps the whole request, so `SET LOCAL app.tenant_id` stays in scope.
- Connection pool sized modestly (10–20). At under 200 users this is nowhere near a constraint.

> **Never call blocking I/O inside an `async def` route.** No `requests`, no `time.sleep`, no
> sync database drivers. One blocking call stalls the event loop for every concurrent request.
> If a blocking library is unavoidable, wrap it in `run_in_threadpool`.

---

## Testing

**Parked** as a v1 requirement. When it starts, the two highest-value targets are the **state
machine** and the **RBAC policy layer** — both are pure logic with no I/O, which makes them the
cheapest things to test and the most expensive to get wrong. Recorded as an accepted risk in
[doc 11](11-decisions-and-risks.md).

The intended shape when it is picked up: `pytest` + `pytest-asyncio`, `httpx.AsyncClient`
against the ASGI app (no live server needed), and a real Postgres for repository tests rather
than SQLite — RLS and Postgres-specific SQL cannot be exercised otherwise.
