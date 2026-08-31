# 02 — Roles & Permissions

## Roles

| Role | Scope | Purpose |
|---|---|---|
| **Platform Super Admin** | Above all tenants | Onboards and deactivates tenants. Has **no access to ticket content** |
| **Tenant Admin** | One tenant | Manages users, teams, clients, and configuration |
| **CS Lead** | One tenant | Supervises support. May override priority, transfer teams, manage clients |
| **CS Agent** | One tenant | Raises tickets, comments, confirms fixes, closes |
| **Team Manager** | One team | Owns a team's inbox, assigns work, may reject and transfer |
| **Developer** | One or more teams | Works assigned tickets. Cannot raise or close tickets |

There is no QA role in v1 — CS performs verification before closing. CS Lead may go unused
until the support team is large enough to need the split.

---

## The three authorization layers

Most systems implement only the middle layer and believe they are done. All three are
required, and they are independent.

```
Request arrives
   |
   +-- Layer 1  TENANT ISOLATION
   |            tenant_id read from the JWT, never from the URL or body,
   |            set as a Postgres session variable, enforced by row-level security.
   |            Not a role question at all.
   |
   +-- Layer 2  PERMISSION (RBAC)
   |            "May this role perform this action at all?"
   |            A FastAPI route dependency: require(Permission.TICKET_ASSIGN)
   |
   +-- Layer 3  RESOURCE POLICY
                "May this user do it to THIS ticket, in THIS state?"
                Assignee? Manager of the owning team? Is the ticket already closed?
                Lives in the service layer, so it applies to every caller.
```

> **Why layer 3 cannot be folded into RBAC:** a rule such as *"a developer may change status,
> but only on tickets assigned to them, and not once the ticket is Closed"* depends on
> ownership and state, not on role. No permission table can express it.

---

## Permission matrix

Permissions are the unit of checking. Roles are named bundles of permissions, so adding a role
later means defining a new bundle rather than editing every endpoint.

| Permission | Super Admin | Tenant Admin | CS Lead | CS Agent | Team Mgr | Developer |
|---|:--:|:--:|:--:|:--:|:--:|:--:|
| `tenant:manage` | yes | | | | | |
| `user:manage` | | yes | | | | |
| `team:manage` | | yes | | | | |
| `client:manage` | | yes | yes | | | |
| `ticket:create` | | yes | yes | yes | yes | |
| `ticket:read` | | yes | yes | yes | yes | yes |
| `ticket:update` | | yes | yes | yes | yes | yes |
| `ticket:assign` | | yes | yes | | yes | self only |
| `ticket:transition` | | yes | yes | | yes | yes |
| `ticket:resolve` | | | | | yes | yes |
| `ticket:close` | | yes | yes | yes | | |
| `ticket:reopen` | | yes | yes | yes | yes | |
| `ticket:reject` | | yes | yes | | yes | yes |
| `ticket:transfer_team` | | yes | yes | | yes | |
| `ticket:override_priority` | | yes | yes | | | |
| `comment:create` | | yes | yes | yes | yes | yes |
| `attachment:upload` | | yes | yes | yes | yes | yes |
| `audit:read` | yes | yes | | | team only | |

The two cells that are not a plain yes or no — `ticket:assign` for a developer, `audit:read`
for a manager — are **layer 3** rules. They are the reason a permission table alone is never
sufficient.

### Where the mapping lives

In code for v1: a constant dictionary, version-controlled, testable, with no per-request query.

*When to move it into the database:* the day a tenant needs custom roles. That is a contained
change, because every check already tests a permission rather than a role name.

---

## Resource policies (layer 3)

Enforced in `modules/tickets/policies.py`, called by the service layer.

| Action | Additional rule beyond the permission |
|---|---|
| Assign | A developer may only assign to themselves, and only from their own team's inbox |
| Transition | Only the current assignee, or the owning team's manager |
| Resolve / Reject | Only the current assignee, or the owning team's manager |
| Close | CS only. Only from `resolved` or `rejected` |
| Update content | Not permitted once the ticket is `closed` |
| Override priority | Requires a non-empty reason |
| Audit read | A team manager sees only their own team's entries |

---

## Navigation visibility

The left navigation is driven by the **permission list** returned from `/auth/me`, not by the
role name. Adding a role, or granting an existing role a new permission, changes the menu with
no frontend change.

| Nav item | Super Admin | Tenant Admin | CS Lead | CS Agent | Team Mgr | Developer |
|---|:--:|:--:|:--:|:--:|:--:|:--:|
| Dashboard | yes | yes | yes | yes | yes | yes |
| My Tickets | | yes | yes | yes | yes | yes |
| Team Inbox | | yes | yes | | yes | yes |
| Awaiting Closure | | yes | yes | yes | | |
| All Tickets | | yes | yes | yes | yes | yes |
| Clients | | yes | yes | | | |
| Teams | | yes | | | | |
| Users | | yes | | | | |
| Audit Log | | yes | | | team only | |
| Tenants | yes | | | | | |

In practice:

```
  Developer            CS Agent             Tenant Admin
  -------------        -----------------    -------------
  Dashboard            Dashboard            Dashboard
  My Tickets           My Tickets           My Tickets
  Team Inbox           Awaiting Closure     Team Inbox
  All Tickets          All Tickets          Awaiting Closure
                                            All Tickets
                                            -------------
                                            Clients
                                            Teams
                                            Users
                                            Audit Log
```

The **New Ticket** button follows the same rule — gated on `ticket:create`, so developers never
see it.

> **Hiding a nav item is UX, not security.** Anyone can type `/users` into the address bar.
> The route guard, the API's 403, and row-level security all still apply independently. Nav
> filtering exists so people are not shown doors they cannot open — not to lock the doors.

---

## Super Admin separation

Platform super admins are stored in a **separate table** from tenant users, with no
`tenant_id`, and they get a different application shell with no ticket navigation at all.

> **Why structural rather than conditional:** an account able to read every tenant's HR support
> data is the highest-value target in the system. Keeping super admins out of the tenant-scoped
> user table means there is no code path where a bug could turn a normal user into a
> cross-tenant one. The isolation is a property of the schema, not of an `if` statement.
