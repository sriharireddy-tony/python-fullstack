# 06 — API

REST over JSON, versioned at `/api/v1`.

## Conventions

| Concern | Decision |
|---|---|
| Style | REST, JSON request and response bodies |
| Versioning | URL path (`/api/v1`) — visible, cacheable, trivially routable. *Alternative:* header versioning, cleaner in theory but harder to debug and to point people at |
| IDs | UUID in the API. `ticket_number` is display-only |
| Timestamps | ISO 8601, UTC, `Z` suffix. Always. Formatting is the frontend's job |
| Field naming | `snake_case` — matches Pydantic, and the generated TypeScript types carry it through |
| Auth | JWT in an httpOnly cookie, plus a CSRF token on state-changing methods |
| Content type | `application/json`, except `multipart/form-data` for uploads |

---

## Actions are endpoints, not a status field

This is the most consequential API decision here.

```
POST /tickets/{id}/assign
POST /tickets/{id}/start
POST /tickets/{id}/hold
POST /tickets/{id}/resolve
POST /tickets/{id}/reject
POST /tickets/{id}/close
POST /tickets/{id}/reopen
POST /tickets/{id}/transfer-team
POST /tickets/{id}/override-priority
```

**Not** `PATCH /tickets/{id}` with `{"status": "closed"}`.

> **Why:** every one of these actions has a different permission, different required fields, and
> different side effects. `close` is CS-only. `reject` requires a reason. `resolve` requires
> resolution notes. `override-priority` requires a justification and writes an audit entry.
>
> A generic status PATCH forces all of that into one branching function, and the permission
> matrix becomes impossible to express at the route level. With explicit endpoints each one
> declares its own `require(...)` dependency and validates its own payload — and the OpenAPI
> schema documents exactly what can be done to a ticket.

`PATCH /tickets/{id}` still exists, but only for **content**: title, description, repro steps,
client, environment. Never status.

---

## Error contract — RFC 9457 Problem Details

```json
{
  "type":   "https://os-tracker/errors/ticket-version-conflict",
  "title":  "Ticket was modified",
  "status": 409,
  "detail": "This ticket was updated by another user. Reload and try again.",
  "code":   "TICKET_VERSION_CONFLICT",
  "request_id": "01J8X2K9QWERTY",
  "errors": [
    { "field": "team_id", "message": "Team is inactive" }
  ]
}
```

> **`code` is the important field.** The frontend switches on a stable machine-readable code,
> never on the human message. Error wording can then be improved, or translated, without
> breaking the UI.

`request_id` appears in the response **and** in the structured logs, so "it failed at 3pm"
becomes a string that finds the exact request.

### Status codes

| Code | Used for |
|---|---|
| 200 | Success with a body |
| 201 | Resource created |
| 204 | Success, no body |
| 400 | Malformed request |
| 401 | Not authenticated, or the token expired |
| 403 | Authenticated but not permitted |
| **404** | Not found — **and also returned instead of 403 for another tenant's resources**, so the API never confirms one exists |
| 409 | Version conflict or an illegal state transition |
| 422 | Validation failure |
| 429 | Rate limited |

---

## Pagination, filtering, sorting

**Offset-based, with a hard cap** (`page`, `page_size`, maximum 100).

> Keyset pagination is more robust at very large scale, but it cannot provide page numbers or a
> total count — both of which this UI needs and users expect. Offset only degrades on very
> large tables.
>
> *When to switch:* a single tenant past a few hundred thousand tickets, or noticeably slow deep
> pages. At the expected volume that is years away, and it is a contained change.

```
GET /api/v1/tickets
    ?status=open&status=assigned
    &team_id=<uuid>&client_id=<uuid>&assignee_id=me
    &priority=p1&environment=production
    &created_from=2026-08-01&created_to=2026-08-31
    &q=payroll+export
    &sort=-created_at
    &page=1&page_size=25
```

```json
{ "items": [ ... ], "total": 143, "page": 1, "page_size": 25 }
```

`assignee_id=me` is supported as a literal — it is the default dashboard view, and it avoids a
round trip to resolve the current user's id. Repeated parameters (`status=open&status=assigned`)
mean OR within a field, AND across fields.

---

## Endpoint catalogue

### Authentication

```
POST   /api/v1/auth/login             sets access + refresh cookies, returns the user
POST   /api/v1/auth/refresh           rotates the refresh token
POST   /api/v1/auth/logout            revokes the token family, clears cookies
POST   /api/v1/auth/change-password   revokes all other sessions
GET    /api/v1/auth/me                current user, role, permissions, teams, preferences
```

> `GET /auth/me` returning the **resolved permission list** is what lets the frontend hide
> buttons and nav items the user cannot use. The backend still enforces everything
> independently — the frontend list is a UX convenience, never a security boundary.

### Tickets

```
GET    /api/v1/tickets                  list, filter, search, paginate
POST   /api/v1/tickets                  create (requires ticket:create)
GET    /api/v1/tickets/{id}
PATCH  /api/v1/tickets/{id}             content only; requires version
GET    /api/v1/tickets/{id}/history     status transitions

POST   /api/v1/tickets/{id}/assign              { assignee_id }
POST   /api/v1/tickets/{id}/start
POST   /api/v1/tickets/{id}/hold                { waiting_on, note }
POST   /api/v1/tickets/{id}/resolve             { resolution_notes }
POST   /api/v1/tickets/{id}/reject              { rejection_reason, note }
POST   /api/v1/tickets/{id}/close               { note? }
POST   /api/v1/tickets/{id}/reopen              { reason }
POST   /api/v1/tickets/{id}/transfer-team       { team_id, reason }
POST   /api/v1/tickets/{id}/override-priority   { priority, reason }
```

Every action body also carries `version` for optimistic locking.

### Comments and attachments

```
GET    /api/v1/tickets/{id}/comments
POST   /api/v1/tickets/{id}/comments        { body }
PATCH  /api/v1/comments/{id}                author only, time-limited
DELETE /api/v1/comments/{id}                soft delete

POST   /api/v1/tickets/{id}/attachments     multipart/form-data
POST   /api/v1/comments/{id}/attachments    multipart/form-data
GET    /api/v1/attachments/{id}/download    permission-checked
DELETE /api/v1/attachments/{id}
```

### Administration

```
GET    /api/v1/clients        POST /api/v1/clients        PATCH /api/v1/clients/{id}
GET    /api/v1/teams          POST /api/v1/teams          PATCH /api/v1/teams/{id}
GET    /api/v1/users          POST /api/v1/users          PATCH /api/v1/users/{id}
POST   /api/v1/users/{id}/deactivate      { reassign_tickets_to }
PATCH  /api/v1/users/me                   profile
PATCH  /api/v1/users/me/preferences       theme, density
GET    /api/v1/audit-logs
```

### Platform (super admin only)

```
GET    /api/v1/platform/tenants     POST /api/v1/platform/tenants
PATCH  /api/v1/platform/tenants/{id}
```

### Meta and operations

```
GET    /api/v1/meta/enums     severities, impacts, workarounds, priorities,
                              statuses, environments, rejection reasons
GET    /health/live           process is up
GET    /health/ready          database and Redis reachable
```

> `/meta/enums` means the frontend never hardcodes a dropdown. Add a rejection reason on the
> backend and it appears in the UI with no frontend deployment.

---

## State machine

Enforced in the service layer, defined once in `tickets/policies.py`.

| From | To | Who |
|---|---|---|
| `open` | `assigned` | Manager, CS Lead, or a developer self-assigning |
| `assigned` | `in_progress`, `on_hold` | Assignee |
| `in_progress` | `on_hold`, `resolved`, `rejected` | Assignee |
| `on_hold` | `in_progress` | Assignee |
| `assigned` | `rejected` | Assignee or Team Manager |
| `resolved` | `closed` | **CS only** |
| `rejected` | `closed` | **CS only** |
| `resolved`, `rejected`, `closed` | `assigned` (reopen) | CS or Manager |
| any non-terminal | `open` (team transfer) | Manager or CS Lead |

Anything absent from this table returns **409** with `code: "INVALID_TRANSITION"`.

---

## Four production details

**Optimistic locking.** Every `PATCH` and action carries the `version` the client read. A
mismatch returns 409 `TICKET_VERSION_CONFLICT`, and the UI offers a reload.

**Idempotency on ticket creation.** `POST /tickets` accepts an `Idempotency-Key` header; the
frontend generates one per form session. A double-click or a retried request returns the
*original* ticket rather than creating a duplicate. Keys live in Redis with a 24-hour TTL.

> CS agents will double-click. This is cheap insurance against duplicate tickets that then have
> to be found and cleaned up.

**Downloads are permission-checked, never guessable.** With local storage, downloads stream
through the application after a permission check. The upload directory is **never** served
statically. On Azure Blob the same endpoint issues a short-lived SAS URL instead — same route,
same check, different implementation behind the storage interface.

**Rate limiting on `/auth/login`** — per IP and per account, with exponential backoff. It is the
only endpoint exposed to unauthenticated traffic, so it is where brute force lands.

---

## Type generation

FastAPI publishes an OpenAPI schema at `/api/v1/openapi.json`. The frontend build runs
`openapi-typescript` against it and writes `frontend/src/api/generated/`.

**Never hand-edit generated types.** Change the Pydantic model, regenerate, and the frontend
fails to compile everywhere that is now wrong. That compile error is the entire point.
