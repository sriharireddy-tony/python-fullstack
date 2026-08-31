# 09 — Logging & Caching

Monitoring, alerting, and log aggregation are **parked** with deployment. Structured logging
inside the application is not — it must be there from the first phase, because retrofitting
correlation ids across an existing codebase is tedious and always incomplete.

---

# Logging

## Format and destination

**Structured JSON to stdout.** No log files, no rotation logic in the application.

> Whatever runs the app later — a container platform, a systemd unit, a VM — collects stdout.
> Keeping the application out of the log-shipping business is what makes the deployment decision
> deferrable without rework.

## Standard fields

Every line carries:

```json
{
  "timestamp": "2026-08-31T09:14:22.481Z",
  "level": "INFO",
  "logger": "app.modules.tickets.service",
  "request_id": "01J8X2K9QWERTY",
  "tenant_id": "…",
  "user_id": "…",
  "method": "POST",
  "path": "/api/v1/tickets/…/assign",
  "status": 200,
  "duration_ms": 34,
  "message": "ticket assigned"
}
```

`request_id` is generated per request — or read from an incoming `X-Request-ID` — and bound to a
`contextvar`, so **every** log line emitted during that request carries it automatically. It is
also returned in every error response.

> A user reports "it failed around 3pm". With a request id in the error they saw, that becomes
> one string that finds every log line for that exact request, across every module it touched.

## Levels

| Level | Used for |
|---|---|
| `DEBUG` | Local development only. Off in every deployed environment |
| `INFO` | Request completion, state transitions, authentication events |
| `WARNING` | Permission denials, version conflicts, rate-limit hits, Redis unavailable and degraded |
| `ERROR` | Unhandled exceptions, with a stack trace |
| `CRITICAL` | Startup failure, database unreachable |

## What must never be logged

This matters more here than in most applications.

> **Never log:** passwords, tokens, cookies, CSRF tokens, or session identifiers. **Never log
> ticket descriptions, comment bodies, or resolution notes** — this is HRMS support data, and
> those fields will contain salaries, personal details, and government identifiers. Never log
> attachment contents or original filenames.
>
> Log the ticket **id**. Never its content.

A log filter redacts known-sensitive keys, so a careless `logger.info(payload)` added during a
future debugging session cannot leak a whole request body. The filter is a safety net, not a
substitute for care at the call site.

## Application logs are not audit logs

| | Application logs | `audit_logs` table |
|---|---|---|
| Purpose | Operational — debugging, performance | Business record — who changed what |
| Lifetime | Short, disposable, sampleable | Permanent |
| Queryable | Only through whatever collects stdout | SQL, exposed in the UI |
| Audience | Engineers | Admins, managers, disputes |

They serve different needs. Do not try to make one do both jobs.

## Events worth an explicit log line

- Login success and failure (failure at `WARNING`, with the account, never the password)
- Logout, token refresh, token-reuse detection
- Every ticket state transition
- Permission denials
- User deactivation and bulk reassignment
- Cache and rate-limiter degradation

## Parked

Log aggregation, dashboards, alerting, error tracking (Sentry or similar), uptime monitoring, and
APM all wait for the deployment decision. Structured JSON with request ids means any of them can
be plugged in later with no application change.

---

# Caching — Redis

**Decided: Redis is part of v1**, with a clear boundary on what it is used for.

## Use Redis for

| Use | Why Redis specifically | TTL |
|---|---|---|
| **Login rate limiting** | Needs a shared counter across processes. The strongest case | Window-based |
| **Idempotency keys** | Natural TTL semantics; exactly what Redis is for | 24 hours |
| **Reference data** — teams, clients, enum metadata | Read on nearly every page, changes maybe weekly | 15 minutes |
| **Revoked access-token denylist** | Sub-millisecond check on every request | Remaining token lifetime |
| **Dashboard counters** | Cheap to recompute, read constantly | 60 seconds |

## Do not cache tickets

> Ticket lists and ticket detail change constantly, are filtered a dozen different ways, and
> each is already a fast indexed Postgres query. Caching them means spending far more effort on
> invalidation than is ever saved — and the failure mode is a user seeing stale ticket state,
> which erodes trust in the whole tool.

## Two rules held firmly

### 1. Every cache key starts with the tenant

```
tenant:{tenant_id}:clients:list
tenant:{tenant_id}:teams:list
tenant:{tenant_id}:dashboard:{user_id}:counts
global:idem:{key}
global:denylist:{jti}
ratelimit:login:ip:{ip}
```

> A cache key without a tenant prefix is the same bug class as a missing `WHERE tenant_id` —
> except row-level security cannot save you, because the database was never consulted.

Enforced by a key-builder function that takes the tenant as its first argument. Keys are never
built by string concatenation at call sites.

### 2. Decide the failure behaviour per use case

Redis will be unavailable at some point.

| Use | Behaviour when Redis is down |
|---|---|
| Reference-data cache | **Fail open** — treat as a miss, query Postgres, log a `WARNING`. The app stays up |
| Dashboard counters | Fail open |
| Idempotency keys | Fail open — a duplicate ticket is recoverable; a rejected creation is not |
| **Rate limiting** | **Fall back to a per-process in-memory limiter** |
| Token denylist | Fail closed for the denylist check — a revoked token must not be honoured |

> Failing fully open on rate limiting would turn a cache outage into an open brute-force window
> on the login endpoint. That is the one place where availability is not the priority.

## Invalidation

**Delete-on-write**, performed by the service that made the change, in the same code path:

```
update a client  ->  delete tenant:{id}:clients:*
update a team    ->  delete tenant:{id}:teams:*
ticket changed   ->  delete tenant:{id}:dashboard:*
```

TTLs act as a backstop, so stale data self-heals within minutes even if an invalidation is
missed. Cache-aside is the only pattern used — read through the cache, write to Postgres, then
invalidate. No write-behind, no cache as a source of truth.

## When Redis earns more

Redis is deliberately present ahead of strict need, because the deferred features all want it:

- **Notifications** — pub/sub, or ARQ as a job queue
- **SLA escalation** — scheduled jobs
- **Background work** of any kind

Having Redis now means those become features rather than infrastructure projects.
