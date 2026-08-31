# 04 — Database

PostgreSQL 16+. Single database, shared schema, `tenant_id` on every tenant-scoped table, with
row-level security as the isolation guarantee.

## Entity map

```
platform_admins                          (deliberately outside all tenants)

tenants ──┬── users ──── user_teams ──── teams
          │     │                          │
          │     └── refresh_tokens         │
          │                                │
          ├── clients                      │
          │                                │
          ├── tenant_counters              │
          │                                │
          └── tickets  <───────────────────┘
                 │   client_id, team_id, assignee_id, created_by_id
                 ├── ticket_status_history
                 ├── comments ────┐
                 └── attachments <┘   (attached to a ticket OR a comment)

audit_logs                               (append-only, spans every entity)
```

---

## Multi-tenancy strategy

**Decided: shared database, shared schema.**

| Approach | Isolation | Cost | Verdict |
|---|---|---|---|
| **Shared DB, shared schema** | Logical, via RLS | Lowest | **Chosen.** One migration path, trivial cross-tenant operational reporting |
| Schema per tenant | Better | Migrations across N schemas become a chore | Rejected |
| Database per tenant | Best | Highest operational cost | Only if a customer contractually demands physical isolation |

*When to revisit:* a tenant with a hard data-residency or physical-isolation requirement moves
to its own database. The shared design does not block that — the application already scopes
every query by tenant.

### Row-level security

`tenant_id` on every row is only half the job. The risk is one forgotten `WHERE tenant_id = ?`
leaking one tenant's data into another. Defence in depth:

```sql
ALTER TABLE tickets ENABLE ROW LEVEL SECURITY;

CREATE POLICY tenant_isolation ON tickets
  USING (tenant_id = current_setting('app.tenant_id')::uuid);
```

Every request opens a transaction and runs:

```sql
SET LOCAL app.tenant_id = '<uuid from the JWT>';
```

`SET LOCAL` matters — it resets when the transaction ends, so a pooled connection cannot leak
one request's tenant context into the next.

> **The gotcha that silently defeats all of this:** RLS is bypassed by superusers **and by the
> table owner**. If the application connects as the same role that ran the migrations, every
> policy above is decorative, and nobody notices until an audit.

**Therefore: two database roles.**

| Role | Rights |
|---|---|
| `os_migrate` | Owns the schema. Used by Alembic only |
| `os_app` | Owns nothing. `SELECT / INSERT / UPDATE` grants only. Subject to RLS |

Plus a CI check that fails the build if any tenant-scoped table lacks `tenant_id` or an RLS
policy. New tables get added under deadline pressure; that test catches the one someone forgot.

---

## Conventions

| Convention | Rule |
|---|---|
| Primary keys | `uuid` (v7 preferred — time-ordered, better index locality) |
| Timestamps | `timestamptz`, always UTC |
| Every table | `created_at`, `updated_at` |
| Most tables | `deleted_at` for soft delete |
| Deletion | **Nothing is hard-deleted.** Rows are flagged and hidden |
| Fixed value sets | Native Postgres enums (status, severity, priority, environment) |
| Admin-managed sets | Lookup tables (teams, clients) |
| Naming | `snake_case`, plural table names, `<entity>_id` foreign keys |

> **Soft delete everywhere:** this system measures people's work. The first time someone
> disputes a reassignment or a priority override, an immutable history is what settles it.

---

## Tables

### `tenants`

| Column | Type | Notes |
|---|---|---|
| `id` | uuid PK | |
| `name` | varchar(200) | |
| `code` | varchar(50) | Unique. Short slug |
| `is_active` | boolean | |
| `created_at` / `updated_at` / `deleted_at` | timestamptz | |

### `platform_admins`

Super admins. **No `tenant_id`.** A separate table from `users` by design — see
[doc 02](02-roles-and-permissions.md).

| Column | Type | Notes |
|---|---|---|
| `id` | uuid PK | |
| `email` | citext | Globally unique |
| `password_hash` | text | Argon2 |
| `full_name` | varchar(200) | |
| `is_active` | boolean | |
| `last_login_at` | timestamptz | |

### `users`

| Column | Type | Notes |
|---|---|---|
| `id` | uuid PK | |
| `tenant_id` | uuid FK | RLS-scoped |
| `email` | citext | **Globally unique** — see the note below |
| `password_hash` | text | Argon2 |
| `full_name` | varchar(200) | |
| `role` | enum | `tenant_admin`, `cs_lead`, `cs_agent`, `team_manager`, `developer` |
| `preferences` | jsonb | `{"theme": "dark", "density": "compact"}` |
| `is_active` | boolean | Deactivation hides the user and revokes their sessions |
| `last_login_at` | timestamptz | |
| `created_at` / `updated_at` / `deleted_at` | timestamptz | |

> **Email is globally unique, not unique per tenant.** Login stays a single field with no
> "which company are you?" step. *Trade-off:* one person cannot hold accounts in two tenants.
> *When to change:* if that becomes real, move to per-tenant uniqueness plus a tenant slug in
> the URL. Unlikely for an internal staff tool.

### `user_teams`

| Column | Type | Notes |
|---|---|---|
| `user_id` | uuid FK | |
| `team_id` | uuid FK | |
| `is_primary` | boolean | |

A join table even though one team per user is expected today — multi-team membership later
needs no migration.

### `refresh_tokens`

| Column | Type | Notes |
|---|---|---|
| `id` | uuid PK | |
| `user_id` | uuid FK | |
| `token_hash` | text | **Hash, never the token itself** |
| `family_id` | uuid | Rotation lineage |
| `expires_at` | timestamptz | |
| `revoked_at` | timestamptz | |
| `replaced_by_id` | uuid | |
| `user_agent` / `ip_address` | text | |

> Storing the hash means a database leak does not hand over live sessions. `family_id`
> implements reuse detection: if an already-rotated token is presented again, that is theft —
> revoke the entire family.

### `teams`

`id`, `tenant_id`, `name`, `code`, `description`, `manager_id` (FK users, nullable),
`is_active`, timestamps. Unique on `(tenant_id, lower(name))`.

Admin-managed, so adding a team is data entry, not a deployment.

### `clients`

`id`, `tenant_id`, `name`, `code`, `tier` (`standard` / `premium` / `enterprise`), `is_active`,
`notes`, timestamps, `deleted_at`. Unique on `(tenant_id, lower(name))`.

`tier` is stored now even though SLA is deferred — one column, and it is what SLA will key off
later.

### `tenant_counters`

| Column | Type | Notes |
|---|---|---|
| `tenant_id` | uuid PK | |
| `last_ticket_number` | integer | |

See "Ticket numbering" below.

---

### `tickets` — the core table

| Column | Type | Notes |
|---|---|---|
| `id` | uuid PK | |
| `tenant_id` | uuid FK | RLS-scoped |
| `ticket_number` | integer | Per-tenant sequence. Displayed as `OS-1042` |
| `title` | varchar(300) | |
| `description` | text | |
| `steps_to_reproduce` | text | |
| `expected_result` | text | |
| `actual_result` | text | |
| `environment` | enum | `production`, `staging`, `uat` |
| `client_id` | uuid FK | **not null** |
| `reporter_name` | varchar(200) | The person at the client who reported it |
| `reporter_email` | varchar(320) | |
| `created_by_id` | uuid FK users | The CS agent |
| `team_id` | uuid FK | **not null** — owning team |
| `assignee_id` | uuid FK | Nullable. Null means it is in the team inbox |
| `severity` | enum | `s1_critical`, `s2_major`, `s3_minor`, `s4_cosmetic` |
| `impact` | enum | `whole_org`, `department`, `few_users`, `single_user` |
| `workaround` | enum | `none`, `painful`, `easy` |
| `priority` | enum | `p1`–`p4`. **Computed and stored** |
| `priority_overridden` | boolean | |
| `priority_override_reason` | text | Required when overridden |
| `priority_overridden_by_id` | uuid FK | |
| `status` | enum | `open`, `assigned`, `in_progress`, `on_hold`, `resolved`, `rejected`, `closed` |
| `on_hold_waiting_on` | enum | Nullable. `cs`, `client`, `other_team` |
| `resolution_notes` | text | |
| `rejection_reason` | enum | Nullable. `not_a_bug`, `duplicate`, `wont_fix`, `cannot_reproduce` |
| `ado_work_item_url` | text | Optional, developer's choice |
| `duplicate_of_id` | uuid FK | **Unused in v1** — a deliberate hedge |
| `reopen_count` | integer | Default 0 |
| `resolved_at` / `closed_at` | timestamptz | |
| `version` | integer | Optimistic locking. Default 1 |
| `search_vector` | tsvector GENERATED | title + description + ticket number |
| `created_at` / `updated_at` / `deleted_at` | timestamptz | |

#### Three decisions worth explaining

**Priority is stored, not computed on read.** Derived at creation from severity, impact, and
workaround. Storing it makes it indexable and filterable, and an override is simply a different
value in the same column. The inputs remain stored, so the rule can be audited or recomputed.

**`ticket_number` needs a real counter, not `MAX(...) + 1`.**

> Two CS agents submitting simultaneously with `MAX() + 1` will produce duplicate numbers — a
> classic race that only appears under real usage. Use the `tenant_counters` row, locked with
> `SELECT ... FOR UPDATE` inside the creation transaction. Not a Postgres sequence, because
> that would require one sequence per tenant.

**`version` gives optimistic locking.** Every update carries the version it read;
`UPDATE ... WHERE version = ?` either matches or affects zero rows, and the API returns "this
ticket changed, reload" instead of silently overwriting a colleague's edit. Without it, a
manager reassigning while a developer changes status means one silently wins — and that bug is
nearly impossible to diagnose from a support report months later.

#### Nullable columns kept deliberately empty

`duplicate_of_id` and the tier field on `clients` are populated by features that do not exist
yet.

> Nullable columns cost nothing today. Retrofitting them onto five thousand existing tickets
> means those tickets are permanently blank, and the reports built on them start from zero.

---

### `ticket_status_history`

`id`, `tenant_id`, `ticket_id`, `from_status`, `to_status`, `changed_by_id`, `note`,
`changed_at`.

One row per transition. This is the free hedge that makes SLA, MTTR, and aging reports possible
later — including for tickets created before those features exist.

> **Why this exists separately from `audit_logs`:** audit is generic with a JSONB payload — fine
> for "who changed what", miserable for "average time from `assigned` to `resolved`, grouped by
> team". A purpose-built, indexed table makes that a straightforward query. Two small tables
> beat one that is awkward for both jobs.

### `comments`

`id`, `tenant_id`, `ticket_id`, `author_id`, `body`, `created_at`, `edited_at`, `deleted_at`.

### `attachments`

`id`, `tenant_id`, `ticket_id` (nullable), `comment_id` (nullable), `uploaded_by_id`,
`original_filename`, `storage_key`, `content_type`, `size_bytes`, `checksum`, `created_at`,
`deleted_at`.

A CHECK constraint enforces that exactly one of `ticket_id` / `comment_id` is set.

> **`storage_key` is opaque** — no paths, no URLs, no filesystem structure encoded in it. That
> is what makes the local-disk to Azure Blob switch a configuration change rather than a data
> migration.

### `audit_logs` — append-only

| Column | Type | Notes |
|---|---|---|
| `id` | uuid PK | |
| `tenant_id` | uuid | Nullable — platform admin actions have none |
| `actor_type` | enum | `user`, `platform_admin`, `system` |
| `actor_id` | uuid | |
| `action` | varchar(100) | `ticket.assigned`, `user.deactivated`, ... |
| `entity_type` | varchar(50) | |
| `entity_id` | uuid | |
| `changes` | jsonb | before / after |
| `ip_address` / `user_agent` | text | |
| `created_at` | timestamptz | |

No updates, no deletes — enforced with a restricted grant, not just convention.

---

## Indexes

```
tickets  (tenant_id, status, team_id, created_at DESC)      team inbox
tickets  (tenant_id, assignee_id, status)                   my tickets
tickets  (tenant_id, client_id, created_at DESC)            filter by client
tickets  (tenant_id, priority, status)                      filter by priority
tickets  UNIQUE (tenant_id, ticket_number)
tickets  GIN (search_vector)                                full-text search
         ^ all of the above partial: WHERE deleted_at IS NULL

ticket_status_history  (ticket_id, changed_at)
comments               (ticket_id, created_at) WHERE deleted_at IS NULL
attachments            (ticket_id), (comment_id)
audit_logs             (tenant_id, entity_type, entity_id, created_at DESC)
refresh_tokens         UNIQUE (token_hash), (user_id)
users                  UNIQUE (lower(email)), (tenant_id, is_active)
teams                  UNIQUE (tenant_id, lower(name))
clients                UNIQUE (tenant_id, lower(name))
```

> **Every composite index leads with `tenant_id`** — not decoration. RLS injects that predicate
> into every query, so an index without it cannot be used efficiently.

---

## Search

Postgres full-text search via a generated `tsvector` column and a GIN index, covering title,
description, and ticket number.

*Why not Elasticsearch:* at low-thousands of tickets per month, Postgres FTS is more than
adequate and adds no operational surface. *When to revisit:* if search quality becomes a
complaint — fuzzy matching, relevance tuning, or search across attachment contents.

---

## Migrations

Alembic, one migration per change, always reviewed by a human — autogenerate is a starting
point, not an answer.

| Rule | Reason |
|---|---|
| Every schema change is a migration | No manual DDL against any environment, ever |
| Migrations run as `os_migrate`, the app runs as `os_app` | Otherwise RLS is bypassed |
| New tenant-scoped tables must add `tenant_id` and an RLS policy **in the same migration** | The CI check enforces it |
| Additive first for column changes | Add, backfill, switch reads, then drop — so a deployment can be rolled back |
