# 01 — Product Requirements

## The problem

Customers using the HRMS product report issues to the customer-support (CS) team. Today there
is no structured system that carries a reported issue from support to the right engineering
team, tracks who owns it, and confirms it was actually fixed. Issues get lost, duplicated, or
silently stall, and support cannot answer "where is this?" without asking someone.

## What OS Tracker does

It is the **system of record for support-originated engineering work**. CS raises a ticket,
it lands in the owning team's inbox, an engineer takes it, works it, and marks it resolved.
CS confirms with the customer and closes it.

```
Customer reports an issue (by phone, email, chat — outside this system)
        |
        v
CS agent raises a ticket in OS Tracker
   selects: client, team, severity, impact, workaround, environment
        |
        v
Ticket appears in that team's inbox (unassigned)
        |
        v
Engineer self-assigns, or the team manager assigns it
        |
        v
Engineer works it  ->  Resolved   (fix done)
                   ->  Rejected   (not a bug / duplicate / will not fix / cannot reproduce)
        |
        v
Ticket returns to the CS queue as "awaiting closure"
CS discusses in the ticket conversation if needed, confirms with the customer
        |
        v
CS closes the ticket
```

**Only CS can close a ticket.** Engineers mark work Resolved or Rejected; they never close.
This is deliberate — see [doc 11](11-decisions-and-risks.md), decision D-07.

---

## Scope

### In scope for v1

| Area | Included |
|---|---|
| Ticket lifecycle | Create, assign, transition, resolve, reject, reopen, close |
| Routing | CS selects the owning team; optional direct assignment to a person |
| Priority | Computed from severity x impact x workaround |
| Collaboration | Threaded comments on a ticket; file attachments |
| Search and filter | Full-text search plus filters on client, team, status, priority, assignee, date |
| Administration | Users, teams, clients, roles |
| Multi-tenancy | Shared database, shared schema, one tenant live at launch |
| Audit | Append-only record of every meaningful change |
| Authentication | Email and password, JWT in httpOnly cookies |
| Authorization | Permission-based RBAC plus resource-level policy |

### Explicitly out of scope

| Excluded | Reason |
|---|---|
| **HubSpot integration** | CS enters tickets manually. Not part of this system |
| **Customer-facing portal** | Customers never log in. They are a data attribute, not users |
| **Notifications** (email, Teams, push) | Deferred. In-app badges and counts substitute |
| **SLA tracking** | Deferred. Priority is the only urgency signal in v1 |
| **Azure DevOps sync** | The ADO link is an optional free-text field, filled at the developer's discretion |
| **Duplicate detection** | Deferred; intended as a later AI-assisted feature |
| **Business hours / working calendars** | Deferred alongside SLA |
| **Reporting dashboards** | Deferred, but the data to build them is captured from day one |
| **Data migration** | There is no existing ticket data to import |
| **Mobile** | Desktop only. Responsive enough not to break on a tablet |
| **AI automation** | Explicitly a later stage, after the application works |

---

## Functional requirements

### Ticket creation

- Only users holding `ticket:create` may raise a ticket. In practice: **CS agents, CS leads,
  tenant admins, and team managers. Developers cannot raise tickets.**
- Mandatory at creation: title, description, **client**, **team**, severity, impact,
  workaround, environment.
- Also captured: steps to reproduce, expected result, actual result, reporter name,
  reporter email.
- CS may optionally assign directly to a person. If they do not, the ticket sits in the team
  inbox until someone takes it or the manager assigns it.
- CS entry time is not a constraint — a thorough form is acceptable and preferred.
- Each ticket receives a human-readable number, unique per tenant, displayed as `OS-1042`.

### Priority

Priority is **never entered directly**. CS answers three factual questions and the system
derives it.

| Severity / Impact | Whole org | Department | Few users | One user |
|---|---|---|---|---|
| **S1 Critical** | P1 | P1 | P2 | P2 |
| **S2 Major** | P1 | P2 | P2 | P3 |
| **S3 Minor** | P2 | P3 | P3 | P4 |
| **S4 Cosmetic** | P3 | P4 | P4 | P4 |

**Workaround modifier:** `none` raises priority one level (capped at P1); `easy` lowers it one
level (floored at P4); `painful` leaves it unchanged.

A CS Lead or Tenant Admin may override the computed priority, but a written reason is
mandatory and the override is recorded.

> **Why derived rather than chosen:** when the person raising a ticket also sets its priority,
> everything becomes P1 within a few months — not through bad faith, but because every agent
> genuinely believes their customer's problem is urgent and nothing costs them for saying so.
> Deriving priority from observable facts removes the lever.

### Ticket lifecycle

```
Open ---> Assigned ---> In Progress ---> Resolved ---> Closed
 ^            |              |  ^            |             |
 |            +--> On Hold --+  |            |             |
 |                              |            |             |
 |                         Rejected ---------+-------------+
 |                                                         |
 +------------------- Reopened <---------------------------+
```

| Status | Meaning |
|---|---|
| `open` | Created, unassigned, sitting in the team inbox |
| `assigned` | Has an assignee, not yet started |
| `in_progress` | Actively being worked |
| `on_hold` | Blocked. Records who is being waited on: CS, client, or another team |
| `resolved` | Engineer believes it is fixed. Awaiting CS confirmation |
| `rejected` | Engineer says no change is needed. Requires a reason. Awaiting CS |
| `closed` | CS has confirmed with the customer. Terminal |

`resolved` and `rejected` are the same *kind* of state: engineering is done, CS must act. Both
appear in the CS "Awaiting Closure" queue, both are discussed in the ticket conversation, and
both are closed by CS.

### Reopening

CS or a manager may reopen a `resolved`, `rejected`, or `closed` ticket. It returns to
`assigned` with the previous assignee, and `reopen_count` increments. A high reopen rate is a
quality signal worth watching later.

### Team transfer

Misrouting is normal, not a failure. Any manager or CS lead may transfer a ticket to a
different team. A reason is required, and the ticket returns to `open` in the new team's inbox.

### Visibility

Visibility is **view scoping, not access control**.

| View | Contents |
|---|---|
| My Tickets (default) | Tickets assigned to the current user |
| Team Inbox | Unassigned tickets for the user's team(s) |
| Awaiting Closure | `resolved` and `rejected` tickets, for CS |
| All Tickets | Every ticket in the tenant — readable, filterable, searchable |

Within a tenant, any user may **read** any ticket. Only the assignee, their team manager, CS,
and admins may **act** on one. Across tenants, nothing is visible under any circumstance.

> **Why reads are not restricted to the assignee:** it would break search and duplicate
> finding, stop colleagues helping each other, and — combined with user deactivation — make a
> departed engineer's open tickets invisible to everyone.

### Collaboration

- **Comments:** threaded conversation on each ticket. All internal; there is no customer view.
  This is where a rejection gets explained and agreed.
- **Attachments:** on tickets and on comments. Screenshots are how bugs get reported.
  10 MB per file, 5 files per ticket, images / PDF / logs / CSV.

### Administration

- **Users:** create, edit, assign role, assign team(s), deactivate. Deactivating a user with
  open tickets **prompts for bulk reassignment** — otherwise those tickets become orphaned.
- **Teams:** admin-managed lookup data, not hardcoded. Launch set: Platform, CoreHR, Payroll,
  UI, Performance, Workforce.
- **Clients:** name, code, tier, active flag. Client selection is mandatory on every ticket,
  which is why free-text client names are not acceptable.
- **Audit log:** read-only view of every recorded change.

---

## Non-functional requirements

| Requirement | Target |
|---|---|
| Users | Under ~200 concurrent at launch; low thousands of tickets per month |
| Browser | Current Chrome and Edge. Desktop only |
| Availability | Business-hours internal tool. No formal uptime commitment yet |
| Data retention | **Parked.** A policy is needed before significant data accumulates |
| Accessibility | Keyboard navigable, sensible contrast in both themes |
| Localisation | English only. Timestamps stored UTC, rendered in the user's local zone |

> The scale figures are deliberately modest, and the architecture reflects that. If real
> volumes turn out to be ten times higher, almost nothing in this design changes — which is
> itself a good sign.

---

## Key assumptions

1. All users are employees of the tenant organisation. There are no external users.
2. Customers ("clients") are recorded as data and never authenticate.
3. One tenant at launch; additional tenants are expected but not scheduled.
4. Engineering continues to use Azure DevOps for implementation work. OS Tracker does not
   replace it, and the link between them is manual and optional.
5. There is no legacy ticket data to migrate.
