"""Ticket state machine and resource-level authorization (layer 3).

Two things live here, and nowhere else:

1. **The transition table.** Every allowed status change, with the permission it
   requires. Anything absent is rejected with 409 INVALID_TRANSITION. Defining
   it once means the rules cannot drift between endpoints.

2. **Resource policies.** Rules that depend on *ownership and state*, which no
   permission table can express -- "a developer may change status, but only on
   tickets assigned to them, and not once the ticket is closed".

Called from the service layer, so the rules apply to every caller: HTTP today,
a background job or automation later.

See docs/02-roles-and-permissions.md and docs/06-api.md.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass

from app.core.errors import InvalidTransitionError, PermissionDeniedError
from app.core.permissions import Permission, Role
from app.modules.tickets.enums import TicketStatus


@dataclass(frozen=True, slots=True)
class Actor:
    """The minimum a policy needs to know about the caller.

    A narrow structure rather than the full request context, so policies stay
    pure and testable without constructing an HTTP request.
    """

    user_id: uuid.UUID
    role: Role | None
    permissions: frozenset[Permission]
    team_ids: frozenset[uuid.UUID]

    def has(self, permission: Permission) -> bool:
        return permission in self.permissions

    @property
    def is_admin(self) -> bool:
        return self.role is Role.TENANT_ADMIN

    @property
    def is_cs(self) -> bool:
        return self.role in (Role.CS_AGENT, Role.CS_LEAD)


@dataclass(frozen=True, slots=True)
class TicketFacts:
    """The ticket state a policy decision depends on."""

    id: uuid.UUID
    status: TicketStatus
    team_id: uuid.UUID
    assignee_id: uuid.UUID | None


#: (from, to) -> permission required.
#:
#: ``open -> assigned`` covers both a manager assigning and a developer taking
#: work from the inbox; the difference between those two is a resource rule
#: (a developer may only assign to themselves), enforced in can_assign().
TRANSITIONS: dict[tuple[TicketStatus, TicketStatus], Permission] = {
    (TicketStatus.OPEN, TicketStatus.ASSIGNED): Permission.TICKET_ASSIGN,
    (TicketStatus.ASSIGNED, TicketStatus.IN_PROGRESS): Permission.TICKET_TRANSITION,
    (TicketStatus.ASSIGNED, TicketStatus.ON_HOLD): Permission.TICKET_TRANSITION,
    (TicketStatus.ASSIGNED, TicketStatus.REJECTED): Permission.TICKET_REJECT,
    (TicketStatus.IN_PROGRESS, TicketStatus.ON_HOLD): Permission.TICKET_TRANSITION,
    (TicketStatus.IN_PROGRESS, TicketStatus.RESOLVED): Permission.TICKET_RESOLVE,
    (TicketStatus.IN_PROGRESS, TicketStatus.REJECTED): Permission.TICKET_REJECT,
    (TicketStatus.ON_HOLD, TicketStatus.IN_PROGRESS): Permission.TICKET_TRANSITION,
    # Only CS closes. A developer knows the code is fixed; only CS knows the
    # client agrees it is fixed. Without this the system would report 100%
    # closure while customers were still complaining.
    (TicketStatus.RESOLVED, TicketStatus.CLOSED): Permission.TICKET_CLOSE,
    (TicketStatus.REJECTED, TicketStatus.CLOSED): Permission.TICKET_CLOSE,
    # Reopen
    (TicketStatus.RESOLVED, TicketStatus.ASSIGNED): Permission.TICKET_REOPEN,
    (TicketStatus.REJECTED, TicketStatus.ASSIGNED): Permission.TICKET_REOPEN,
    (TicketStatus.CLOSED, TicketStatus.ASSIGNED): Permission.TICKET_REOPEN,
}

#: A team transfer returns the ticket to the new team's inbox from any
#: non-terminal state, so it is expressed separately from the table above.
TRANSFERABLE_FROM: frozenset[TicketStatus] = frozenset(
    {
        TicketStatus.OPEN,
        TicketStatus.ASSIGNED,
        TicketStatus.IN_PROGRESS,
        TicketStatus.ON_HOLD,
    }
)

TERMINAL_STATUSES: frozenset[TicketStatus] = frozenset({TicketStatus.CLOSED})


def assert_transition_allowed(actor: Actor, ticket: TicketFacts, to_status: TicketStatus) -> None:
    """Check a status change against the table, then against ownership."""
    required = TRANSITIONS.get((ticket.status, to_status))
    if required is None:
        raise InvalidTransitionError(
            f"A ticket cannot move from {ticket.status.value} to {to_status.value}."
        )

    if not actor.has(required):
        raise PermissionDeniedError("You do not have permission to perform this action.")

    _assert_can_act_on(actor, ticket, to_status)


def _assert_can_act_on(actor: Actor, ticket: TicketFacts, to_status: TicketStatus) -> None:
    """Ownership and state rules -- the part RBAC cannot express."""
    if actor.is_admin:
        return

    # Closing and reopening are CS actions and are not tied to team ownership:
    # CS raised the ticket and speaks to the client.
    if to_status is TicketStatus.CLOSED or (
        to_status is TicketStatus.ASSIGNED
        and ticket.status
        in (
            TicketStatus.RESOLVED,
            TicketStatus.REJECTED,
            TicketStatus.CLOSED,
        )
    ):
        return

    is_assignee = ticket.assignee_id == actor.user_id
    manages_team = actor.role is Role.TEAM_MANAGER and ticket.team_id in actor.team_ids

    if to_status is TicketStatus.ASSIGNED and ticket.status is TicketStatus.OPEN:
        return  # narrowed by can_assign()

    if not (is_assignee or manages_team):
        raise PermissionDeniedError(
            "Only the assignee or the owning team's manager can change this ticket."
        )


def assert_can_assign(actor: Actor, ticket: TicketFacts, assignee_id: uuid.UUID | None) -> None:
    """Assignment rules.

    A developer holds ``ticket:assign`` but may only ever assign to themselves,
    and only within their own team. That narrowing is why the permission table
    alone is never sufficient.
    """
    if not actor.has(Permission.TICKET_ASSIGN):
        raise PermissionDeniedError("You do not have permission to assign tickets.")

    if ticket.status in TERMINAL_STATUSES:
        raise InvalidTransitionError("A closed ticket cannot be assigned. Reopen it first.")

    if actor.is_admin or actor.role in (Role.CS_LEAD, Role.TEAM_MANAGER):
        if actor.role is Role.TEAM_MANAGER and ticket.team_id not in actor.team_ids:
            raise PermissionDeniedError("You can only assign tickets owned by your own team.")
        return

    if actor.role is Role.DEVELOPER:
        if assignee_id != actor.user_id:
            raise PermissionDeniedError("You can only assign tickets to yourself.")
        if ticket.team_id not in actor.team_ids:
            raise PermissionDeniedError("You can only take tickets from your own team's inbox.")
        return

    raise PermissionDeniedError("You do not have permission to assign tickets.")


def assert_can_edit_content(actor: Actor, ticket: TicketFacts) -> None:
    """Content edits: title, description, repro fields, client, environment."""
    if not actor.has(Permission.TICKET_UPDATE):
        raise PermissionDeniedError("You do not have permission to edit tickets.")

    if ticket.status in TERMINAL_STATUSES:
        raise InvalidTransitionError("A closed ticket cannot be edited. Reopen it first.")

    if actor.is_admin or actor.is_cs:
        return

    is_assignee = ticket.assignee_id == actor.user_id
    manages_team = actor.role is Role.TEAM_MANAGER and ticket.team_id in actor.team_ids
    if not (is_assignee or manages_team):
        raise PermissionDeniedError(
            "Only CS, the assignee, or the owning team's manager can edit this ticket."
        )


def assert_can_transfer_team(actor: Actor, ticket: TicketFacts) -> None:
    if not actor.has(Permission.TICKET_TRANSFER_TEAM):
        raise PermissionDeniedError("You do not have permission to transfer tickets.")
    if ticket.status not in TRANSFERABLE_FROM:
        raise InvalidTransitionError("Only an open ticket can be transferred to another team.")


def assert_can_override_priority(actor: Actor, ticket: TicketFacts) -> None:
    if not actor.has(Permission.TICKET_OVERRIDE_PRIORITY):
        raise PermissionDeniedError("You do not have permission to override priority.")
    if ticket.status in TERMINAL_STATUSES:
        raise InvalidTransitionError("A closed ticket's priority cannot be changed.")


def assert_can_comment(actor: Actor, ticket: TicketFacts) -> None:
    """Commenting is open to everyone in the tenant who can read the ticket.

    Deliberately permissive: the conversation is where a rejection gets
    explained and agreed, and restricting it would break that flow.
    """
    if not actor.has(Permission.COMMENT_CREATE):
        raise PermissionDeniedError("You do not have permission to comment.")
    if ticket.status in TERMINAL_STATUSES and not (actor.is_cs or actor.is_admin):
        raise InvalidTransitionError("A closed ticket cannot be commented on.")


def available_actions(actor: Actor, ticket: TicketFacts) -> list[str]:
    """Actions this actor may take on this ticket, in its current state.

    Drives the contextual buttons in the UI. The server still enforces each
    action independently -- this is a convenience, never a gate.
    """
    actions: list[str] = []

    for (from_status, to_status), permission in TRANSITIONS.items():
        if from_status is not ticket.status or not actor.has(permission):
            continue
        try:
            _assert_can_act_on(actor, ticket, to_status)
        except (PermissionDeniedError, InvalidTransitionError):
            continue
        actions.append(_ACTION_NAMES[(from_status, to_status)])

    if ticket.status in TRANSFERABLE_FROM and actor.has(Permission.TICKET_TRANSFER_TEAM):
        actions.append("transfer-team")
    if ticket.status not in TERMINAL_STATUSES and actor.has(Permission.TICKET_OVERRIDE_PRIORITY):
        actions.append("override-priority")

    return sorted(set(actions))


_ACTION_NAMES: dict[tuple[TicketStatus, TicketStatus], str] = {
    (TicketStatus.OPEN, TicketStatus.ASSIGNED): "assign",
    (TicketStatus.ASSIGNED, TicketStatus.IN_PROGRESS): "start",
    (TicketStatus.ASSIGNED, TicketStatus.ON_HOLD): "hold",
    (TicketStatus.ASSIGNED, TicketStatus.REJECTED): "reject",
    (TicketStatus.IN_PROGRESS, TicketStatus.ON_HOLD): "hold",
    (TicketStatus.IN_PROGRESS, TicketStatus.RESOLVED): "resolve",
    (TicketStatus.IN_PROGRESS, TicketStatus.REJECTED): "reject",
    (TicketStatus.ON_HOLD, TicketStatus.IN_PROGRESS): "start",
    (TicketStatus.RESOLVED, TicketStatus.CLOSED): "close",
    (TicketStatus.REJECTED, TicketStatus.CLOSED): "close",
    (TicketStatus.RESOLVED, TicketStatus.ASSIGNED): "reopen",
    (TicketStatus.REJECTED, TicketStatus.ASSIGNED): "reopen",
    (TicketStatus.CLOSED, TicketStatus.ASSIGNED): "reopen",
}

# Every transition must have a display name, or available_actions() would raise
# KeyError the first time a user opens a ticket in that state.
assert set(_ACTION_NAMES) == set(TRANSITIONS)
