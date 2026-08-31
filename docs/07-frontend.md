# 07 — Frontend

React 18+ with TypeScript, built by Vite. Single-page application, desktop only, behind a login.

## Layout

```
┌──────────────────────────────────────────────────────────────┐
│ ☰  OS Tracker      [ search ⌕ ]       + New Ticket   Srihari ▾│  top nav
├────────────┬─────────────────────────────────────────────────┤
│            │                                                 │
│ Dashboard  │   Page content                                  │
│ My Tickets │                                                 │
│ Team Inbox │                                                 │
│ All Tickets│                                                 │
│ ─────────  │                                                 │
│ Clients    │                                                 │
│ Teams      │                                                 │
│ Users      │                                                 │
│ Audit Log  │                                                 │
│            │                                                 │
└────────────┴─────────────────────────────────────────────────┘
   left nav
   collapsible to an icon rail
```

### Top nav

- Application name and sidebar toggle
- Global search — jumps straight to a ticket by number, or into filtered search
- **New Ticket** button, gated on `ticket:create` (so developers never see it)
- User menu

### User menu

```
  Srihari P
  Developer · Payroll
  ─────────────────────
  👤  My Profile
  🔑  Change Password
  ─────────────────────
  🎨  Theme      Light | Dark | System
  ▤   Density    Comfortable | Compact
  ─────────────────────
  ⏻   Log out
```

### Left nav is permission-driven

One declarative array, filtered against the permission list from `/auth/me`:

```ts
const NAV = [
  { label: 'Dashboard',        path: '/',                 permission: null },
  { label: 'My Tickets',       path: '/tickets/mine',     permission: 'ticket:read' },
  { label: 'Team Inbox',       path: '/tickets/inbox',    permission: 'ticket:assign' },
  { label: 'Awaiting Closure', path: '/tickets/awaiting', permission: 'ticket:close' },
  { label: 'All Tickets',      path: '/tickets',          permission: 'ticket:read' },
  { label: 'Clients',          path: '/clients',          permission: 'client:manage' },
  { label: 'Teams',            path: '/teams',            permission: 'team:manage' },
  { label: 'Users',            path: '/users',            permission: 'user:manage' },
  { label: 'Audit Log',        path: '/audit',            permission: 'audit:read' },
]
```

> The effect is role-based, but the mechanism is permission-based. Granting CS Leads access to
> Teams later changes the permission map in one place and the nav follows — no
> `if (role === 'developer')` scattered through the layout.

Per-role navigation is tabulated in [doc 02](02-roles-and-permissions.md).

**Super admins get a different shell entirely** — Tenants, Platform Admins, and their own audit
log. No ticket navigation at all.

---

## Theme and density

Stored in **two places, deliberately**:

| Location | Role |
|---|---|
| `users.preferences` (server) | Source of truth. Follows the person to another machine |
| `localStorage` | Read synchronously before React mounts, applied to `<html data-bs-theme>` |

The localStorage copy exists purely to prevent a flash of white before the API responds. It is
a cache, not the source of truth; the server value wins on load.

Bootstrap 5.3 has native dark mode through `data-bs-theme`, so this is one attribute on the root
element rather than a parallel stylesheet. Anything themed by hand uses CSS custom properties so
both modes stay in sync.

**Density (comfortable / compact)** matters more than it sounds — this application is mostly
tables, and a developer scanning fifty tickets wants compact rows. It is a class on the table
container driving row padding.

---

## Folder structure

```
frontend/src/
├─ api/
│  ├─ generated/          openapi-typescript output — never hand-edited
│  ├─ client.ts           fetch wrapper: credentials, CSRF header, error mapping
│  └─ hooks/              useTickets, useTicket, useAssignTicket, ...
├─ features/
│  ├─ auth/               login, change password, route guards
│  ├─ tickets/            list, detail, create, comments, history, actions
│  ├─ clients/
│  ├─ teams/
│  ├─ users/
│  ├─ audit/
│  └─ profile/            profile and preferences
├─ components/            Table, Modal, Badge, EmptyState, ErrorState, Toast, Confirm
├─ layout/                AppShell, TopNav, SideNav, UserMenu
├─ lib/                   permissions, formatting, date helpers, constants
└─ routes/                route definitions and guards
```

Feature folders mirror backend modules on purpose: a change to ticket rejection touches
`backend/app/modules/tickets/` and `frontend/src/features/tickets/`, and nothing else.

---

## State — three kinds, three tools

| Kind | Tool | Examples |
|---|---|---|
| Server state | **TanStack Query** | tickets, clients, users, current user |
| URL state | **React Router search params** | filters, page, sort |
| Local UI state | `useState`, small Contexts | modals, theme, auth session |

> **Filters belong in the URL, not in React state.** `?status=open&team_id=...&page=2` makes a
> filtered view bookmarkable, shareable in chat, and survivable across a refresh. The team
> *will* paste filtered links to each other — that is how people use a ticket tool. Filters held
> only in component state make that impossible, and it is awkward to retrofit.

**Query key convention:** `['tickets', filters]`, `['ticket', id]`, `['ticket', id, 'comments']`.
Mutations invalidate the narrowest key that could have changed.

---

## Screens

| Route | Screen | Notes |
|---|---|---|
| `/login` | Login | Email and password. No app shell |
| `/` | Dashboard | Role-aware counts and shortcuts into saved views |
| `/tickets` | All tickets | Full filter set |
| `/tickets/mine` | My tickets | Default landing view for most roles |
| `/tickets/inbox` | Team inbox | Unassigned, for the user's teams |
| `/tickets/awaiting` | Awaiting closure | `resolved` + `rejected`, for CS |
| `/tickets/new` | Create ticket | `ticket:create` only |
| `/tickets/:id` | Ticket detail | Tabbed |
| `/clients`, `/teams`, `/users` | Admin lists | Table plus edit modal |
| `/audit` | Audit log | Read-only, filterable |
| `/profile` | Profile and preferences | |

### Ticket list — the screen that matters most

TanStack Table (headless) with Bootstrap styling. **Server-side everything**: filtering,
sorting, and pagination are all query parameters.

> Never fetch all tickets and filter client-side. It works beautifully with 50 tickets and dies
> at 5,000.

Columns: `#` · Title · Client · Team · Assignee · Priority · Status · Age · Updated.

Priority and status render as coloured badges — P1 red through P4 grey. In a table of forty
rows, colour is what makes scanning possible.

**Saved views** are URL presets in the left nav: *My Open*, *Team Inbox*, *Awaiting Closure*,
*Unassigned*.

### Ticket detail

```
OS-1042  Payroll export fails for Acme            [P1] [In Progress]
──────────────────────────────────────────────────────────────────
 Details │ Conversation (7) │ Attachments (2) │ History
──────────────────────────────────────────────────────────────────
 left   description, steps to reproduce, expected vs actual
 right  sidebar — client, reporter, team, assignee, severity, impact,
        environment, ADO link, created/updated timestamps
 footer contextual action buttons
```

**Conversation is where a rejection gets resolved** — the developer rejects with a reason and
explains, CS replies, CS closes. That flow lives in this tab.

**Action buttons render from permissions *and* current state.** A developer viewing a `resolved`
ticket sees nothing actionable; CS sees *Close* and *Reopen*. The same policy table as the
backend, expressed once in `lib/permissions.ts` — with the backend enforcing independently.

---

## Conventions

- **Every list has three states**: loading skeleton, empty state with a next action, and error
  state with retry. Decided once as shared components, then never thought about again.
- **Toasts for every mutation.** With no notifications in v1, in-app feedback is the *only*
  confirmation a user gets that something happened.
- **Optimistic updates only for cheap, safe actions** (self-assign). Not for close or reject —
  those can fail on a version conflict, and rolling back a "Closed" badge is confusing.
- **Version conflicts get a real dialogue**, not a generic error: "This ticket was changed by
  someone else. Reload to see the latest."
- **Route guards** redirect unauthorized deep links rather than rendering a broken page.
- **Confirmation dialogs** on destructive or hard-to-reverse actions: close, reject, deactivate
  a user.
- **Dates** render relative for recency ("2 hours ago") with the absolute UTC-converted local
  time on hover.

---

## Accessibility and browser support

Chrome and Edge, current versions, desktop. Keyboard navigation through lists and forms, visible
focus rings, and contrast checked in both light and dark themes. No screen-reader certification
target for v1.
