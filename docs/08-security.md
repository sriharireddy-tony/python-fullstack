# 08 — Security

This application stores support tickets for an HR product. Attachments and descriptions will
contain payroll figures, employee personal data, and potentially government identifiers. That
makes data protection a first-class requirement rather than a later hardening pass.

---

## Authentication

**Decided: JWT as the token format, delivered in httpOnly cookies.**

These are not competing choices — JWT is the *format*, cookie versus localStorage is the
*transport*.

| | localStorage + Authorization header | httpOnly cookie |
|---|---|---|
| Readable by XSS | **Yes** — any injected script steals the token | No — JavaScript cannot access it |
| CSRF risk | No | Yes, mitigated by SameSite plus a CSRF token |
| Cross-domain and mobile clients | Easy | Needs configuration |

> This application renders user-supplied ticket titles, descriptions, and comments. That is
> precisely where XSS comes from. A token in localStorage means one missed escape anywhere in
> the app leaks every logged-in session. The cookie removes that entire path.

*When to add bearer tokens:* the day a mobile app or third-party API client exists. Add them
alongside the cookie; do not replace it.

### Token design

| | Access token | Refresh token |
|---|---|---|
| Lifetime | 15 minutes | 7 days, rotating |
| Transport | httpOnly, Secure, SameSite=Lax cookie | httpOnly cookie scoped to the refresh path |
| Claims | `sub`, `tenant_id`, `role`, `jti`, `exp` | `sub`, `jti`, `exp` |
| Server state | None (denylist in Redis) | Row in `refresh_tokens` |

**Put the role in the token, not the permission list.**

> If permissions are embedded and a role's permissions later change, every issued token is stale
> until it expires. Storing only the role means permissions resolve server-side on every request
> from a single authoritative source — a dictionary lookup, and changes take effect immediately.

### Revocation

A signed JWT is valid until it expires, even after a user is deactivated. Two mitigations, both
required:

1. **Short access-token lifetime** (15 minutes) bounds the damage window.
2. **A refresh-token table in Postgres.** Logout, deactivation, or a password change deletes the
   row, so the session cannot be renewed.

Rotate on every refresh, and treat reuse of an already-rotated token as theft: revoke the whole
token family.

For immediate revocation, the `jti` of a revoked access token goes into a Redis denylist with a
TTL equal to its remaining lifetime.

### Passwords

- Argon2id hashing, never anything reversible.
- Minimum length enforced; no arbitrary composition rules.
- **Changing a password revokes every other session** for that user.
- Login failures return one generic message — never "no such user" versus "wrong password",
  which would confirm which accounts exist.

### CSRF

Because authentication rides on cookies:

- `SameSite=Lax` on all auth cookies (blocks the common cross-site cases).
- A double-submit CSRF token on every `POST`, `PATCH`, `PUT`, `DELETE`: a readable cookie value
  echoed in an `X-CSRF-Token` header.
- Safe methods (`GET`, `HEAD`) require no token.

---

## Authorization

Three independent layers, described fully in [doc 02](02-roles-and-permissions.md):

1. **Tenant isolation** — `tenant_id` from the token, set as a Postgres session variable,
   enforced by row-level security.
2. **Permission check (RBAC)** — a route dependency, before the handler runs.
3. **Resource policy** — ownership and state rules, in the service layer.

> A failure in any one layer is caught by the others. Most systems implement only layer 2.

**Frontend permission checks are UX, not security.** Hidden buttons and filtered nav items exist
so people are not shown doors they cannot open. Every check is repeated server-side.

---

## Tenant isolation

The highest-severity bug class in this system is a cross-tenant data leak. Defences:

| Layer | Mechanism |
|---|---|
| Database | RLS policy on every tenant-scoped table |
| Connection | The app connects as `os_app`, which owns nothing — **table owners bypass RLS** |
| Request | `SET LOCAL app.tenant_id` inside the transaction, sourced only from the token |
| API | Cross-tenant resources return **404**, never 403 — existence is not confirmed |
| Cache | Every Redis key is prefixed with the tenant id |
| CI | A test fails the build if any tenant-scoped table lacks `tenant_id` or an RLS policy |

> **Tenant id is never accepted from client input** — not from a path parameter, a query string,
> a request body, or a header. A request cannot ask to act on another tenant.

---

## Data protection

### What this system holds

Ticket descriptions, comments, and attachments contain third-party employee data: names,
salaries, leave records, identifiers. It is not the tenant's own data — it belongs to their
customers' employees.

### Controls

| Control | Implementation |
|---|---|
| Transport | HTTPS only. HSTS. Secure cookies |
| At rest | Database and blob encryption — a hosting concern, **parked** with deployment |
| Access | Every read is permission-checked and tenant-scoped |
| Audit | Every meaningful change recorded in an append-only log |
| Logging | Ticket and comment bodies are **never** logged. See [doc 09](09-logging-and-caching.md) |
| Deletion | Soft delete only. A retention and purge policy is **parked** |
| Exports | Not built in v1. When added, exports must be audited |

> **Data retention is an open policy decision, not a technical one** — but the schema already
> carries the hooks (`deleted_at`, timestamps) so a purge job is additive. It needs an answer
> before three years of payroll screenshots accumulate.

---

## File uploads

| Control | Rule |
|---|---|
| Size | 10 MB per file, 5 files per ticket |
| Type | Allowlist by extension **and** content-type sniffing; never trust the client |
| Filename | Original stored as metadata only. The storage key is a generated opaque id |
| Storage path | Never derived from user input — no path traversal surface |
| Serving | Streamed through a permission-checked endpoint. The upload directory is **never** served statically |
| Download headers | `Content-Disposition: attachment` and `X-Content-Type-Options: nosniff` so an uploaded HTML file cannot execute in the app's origin |
| Virus scanning | **Not implemented.** Accepted risk — internal users only. Revisit before any external upload path |

---

## Input handling and injection

| Risk | Mitigation |
|---|---|
| SQL injection | SQLAlchemy parameterised queries throughout. No string-built SQL |
| XSS | React escapes by default. **No `dangerouslySetInnerHTML`** on user content anywhere |
| Mass assignment | Pydantic schemas are explicit allowlists; ORM models are never bound to request bodies |
| Oversized payloads | Body size limits on all endpoints |
| Enumeration | 404 rather than 403 for out-of-scope resources; generic login errors |

---

## Rate limiting

| Endpoint | Limit |
|---|---|
| `POST /auth/login` | Per IP **and** per account, with exponential backoff |
| `POST /auth/refresh` | Per account |
| Write endpoints | A generous per-user ceiling |
| Uploads | Per user, per hour |

Backed by Redis. **If Redis is unavailable, rate limiting falls back to a per-process in-memory
limiter** — failing fully open would turn a cache outage into an open brute-force window on the
login endpoint.

---

## Secrets and configuration

- No secrets in the repository, ever. `.env.example` is committed; `.env` is not.
- All configuration through environment variables.
- The application **fails to start** if a required secret is missing.
- `JWT_SECRET` is rotatable — rotation invalidates all sessions, which is acceptable and
  occasionally desirable.
- Database credentials differ per role: `os_migrate` for Alembic, `os_app` for the application.

---

## HTTP security headers

```
Strict-Transport-Security: max-age=31536000; includeSubDomains
X-Content-Type-Options: nosniff
X-Frame-Options: DENY
Referrer-Policy: strict-origin-when-cross-origin
Content-Security-Policy: default-src 'self'; frame-ancestors 'none'
```

CORS is restricted to the known frontend origin, with credentials allowed. Never a wildcard —
wildcards and cookie credentials are incompatible anyway.

---

## Accepted security risks for v1

Recorded so they are decisions rather than oversights. Full list in
[doc 11](11-decisions-and-risks.md).

| Risk | Rationale | Revisit when |
|---|---|---|
| No virus scanning on uploads | Internal users only | Any external or customer-facing upload |
| No automated security tests | Testing is parked | Testing phase begins |
| No MFA | Internal tool, no external exposure yet | Internet-exposed, or a second tenant onboards |
| No encryption at rest specified | Depends on hosting, which is parked | Deployment is decided |
| No penetration test | Pre-launch | Before the first real tenant goes live |
