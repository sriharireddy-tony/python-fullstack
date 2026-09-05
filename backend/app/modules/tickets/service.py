"""Ticket business rules.

Every mutation follows the same shape:

  1. Load the ticket (404 if absent -- including for another tenant, so the API
     never confirms a resource exists elsewhere).
  2. Check the optimistic-locking version.
  3. Ask ``policies`` whether this actor may do this, to this ticket, in this
     state.
  4. Apply the change, write status history and an audit entry **in the same
     transaction**, and invalidate affected caches.

Steps 3 and 4 are why this lives in a service rather than a route handler: a
background job or future automation gets identical rules for free.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.cache import Cache, CacheKey
from app.core.errors import NotFoundError, ValidationFailedError, VersionConflictError
from app.core.events import DomainEvent, emit
from app.core.logging import get_logger
from app.core.pagination import Page, PageParams
from app.modules.audit.models import AuditAction
from app.modules.audit.service import AuditService
from app.modules.tickets import policies
from app.modules.tickets.enums import (
    Priority,
    RejectionReason,
    TicketStatus,
    WaitingOn,
    derive_priority,
)
from app.modules.tickets.models import Ticket, TicketStatusHistory
from app.modules.tickets.policies import Actor, TicketFacts
from app.modules.tickets.repository import TicketFilters, TicketRepository
from app.modules.tickets.schemas import TicketContentUpdate, TicketCreate

logger = get_logger(__name__)

CONTENT_FIELDS = (
    "title",
    "description",
    "steps_to_reproduce",
    "expected_result",
    "actual_result",
    "environment",
    "client_id",
    "reporter_name",
    "reporter_email",
    "ado_work_item_url",
)


@dataclass(frozen=True, slots=True)
class RequestMeta:
    ip_address: str | None = None
    user_agent: str | None = None


class TicketService:
    def __init__(
        self,
        session: AsyncSession,
        tenant_id: uuid.UUID,
        cache: Cache | None = None,
    ) -> None:
        self._session = session
        self._tenant_id = tenant_id
        self._repo = TicketRepository(session)
        self._audit = AuditService(session)
        self._cache = cache or Cache()

    # ------------------------------------------------------------- reads

    async def search(
        self,
        filters: TicketFilters,
        params: PageParams,
        *,
        sort_field: str = "created_at",
        descending: bool = True,
    ) -> Page[Ticket]:
        return await self._repo.search(
            filters, params, sort_field=sort_field, descending=descending
        )

    async def get(self, ticket_id: uuid.UUID) -> Ticket:
        ticket = await self._repo.get(ticket_id)
        if ticket is None:
            raise NotFoundError("Ticket not found.")
        return ticket

    async def get_by_reference(self, reference: str) -> Ticket:
        """Resolve ``OS-1042`` or a bare number, so URLs can carry either."""
        raw = reference[3:] if reference.lower().startswith("os-") else reference
        if not raw.isdigit():
            raise NotFoundError("Ticket not found.")
        ticket = await self._repo.get_by_number(int(raw))
        if ticket is None:
            raise NotFoundError("Ticket not found.")
        return ticket

    async def history(self, ticket_id: uuid.UUID) -> list[TicketStatusHistory]:
        await self.get(ticket_id)  # 404 before exposing history
        return await self._repo.history(ticket_id)

    async def dashboard_counts(
        self, *, user_id: uuid.UUID, team_ids: list[uuid.UUID]
    ) -> dict[str, int]:
        key = CacheKey.dashboard_counts(self._tenant_id, user_id)
        cached = await self._cache.get_json(key)
        if isinstance(cached, dict):
            return {str(k): int(v) for k, v in cached.items()}

        counts = await self._repo.counts_for_dashboard(user_id=user_id, team_ids=team_ids)
        from app.core.cache import TTL

        await self._cache.set_json(key, counts, TTL.DASHBOARD_COUNTS)
        return counts

    # ------------------------------------------------------------ create

    async def create(
        self, actor: Actor, payload: TicketCreate, meta: RequestMeta | None = None
    ) -> Ticket:
        meta = meta or RequestMeta()

        priority = derive_priority(payload.severity, payload.impact, payload.workaround)
        number = await self._repo.next_ticket_number(self._tenant_id)

        status = TicketStatus.OPEN
        if payload.assignee_id is not None:
            status = TicketStatus.ASSIGNED

        ticket = Ticket(
            tenant_id=self._tenant_id,
            ticket_number=number,
            title=payload.title.strip(),
            description=payload.description,
            steps_to_reproduce=payload.steps_to_reproduce,
            expected_result=payload.expected_result,
            actual_result=payload.actual_result,
            environment=payload.environment,
            client_id=payload.client_id,
            reporter_name=payload.reporter_name,
            reporter_email=str(payload.reporter_email) if payload.reporter_email else None,
            created_by_id=actor.user_id,
            team_id=payload.team_id,
            assignee_id=payload.assignee_id,
            severity=payload.severity,
            impact=payload.impact,
            workaround=payload.workaround,
            priority=priority,
            status=status,
        )
        await self._repo.add(ticket)

        await self._record_transition(ticket, None, status, actor.user_id, note="Created")
        await self._audit.record(
            tenant_id=self._tenant_id,
            actor_id=actor.user_id,
            action=AuditAction.TICKET_CREATED,
            entity_type="ticket",
            entity_id=ticket.id,
            after={
                "ticket_number": number,
                "priority": priority.value,
                "severity": payload.severity.value,
                "impact": payload.impact.value,
                "team_id": str(payload.team_id),
                "client_id": str(payload.client_id),
            },
            ip_address=meta.ip_address,
            user_agent=meta.user_agent,
        )
        await self._invalidate_dashboards()

        # Emitted rather than calling the AI layer directly: tickets must not
        # depend on intelligence. The handler writes its outbox row in THIS
        # transaction, so the job and the ticket commit together.
        await emit(
            DomainEvent.TICKET_CREATED,
            self._session,
            self._tenant_id,
            ticket.id,
        )

        logger.info(
            "ticket created",
            extra={
                "ticket_id": str(ticket.id),
                "ticket_number": number,
                "priority": priority.value,
            },
        )
        return ticket

    # ------------------------------------------------------ content edit

    async def update_content(
        self,
        actor: Actor,
        ticket_id: uuid.UUID,
        payload: TicketContentUpdate,
        meta: RequestMeta | None = None,
    ) -> Ticket:
        ticket = await self.get(ticket_id)
        self._assert_version(ticket, payload.version)
        policies.assert_can_edit_content(actor, ticket_facts(ticket))

        before: dict[str, Any] = {}
        after: dict[str, Any] = {}
        updates = payload.model_dump(exclude_unset=True, exclude={"version"})

        for field_name, value in updates.items():
            if field_name not in CONTENT_FIELDS or value is None:
                continue
            current = getattr(ticket, field_name)
            if current == value:
                continue
            before[field_name] = str(current) if current is not None else None
            after[field_name] = str(value)
            setattr(ticket, field_name, value)

        if not after:
            return ticket  # nothing changed; do not burn a version

        ticket.version += 1
        await self._session.flush()

        await self._audit.record(
            tenant_id=self._tenant_id,
            actor_id=actor.user_id,
            action=AuditAction.TICKET_UPDATED,
            entity_type="ticket",
            entity_id=ticket.id,
            before=before,
            after=after,
            ip_address=(meta or RequestMeta()).ip_address,
        )

        # Only re-embed when embedded fields changed. source_hash would skip a
        # no-op anyway, but not enqueueing avoids the pointless job entirely.
        if any(field in after for field in ("title", "description", "steps_to_reproduce")):
            await emit(
                DomainEvent.TICKET_CONTENT_CHANGED,
                self._session,
                self._tenant_id,
                ticket.id,
            )
        return ticket

    # ----------------------------------------------------------- actions

    async def assign(
        self,
        actor: Actor,
        ticket_id: uuid.UUID,
        version: int,
        assignee_id: uuid.UUID | None,
    ) -> Ticket:
        ticket = await self.get(ticket_id)
        self._assert_version(ticket, version)
        facts = ticket_facts(ticket)
        policies.assert_can_assign(actor, facts, assignee_id)

        previous = ticket.assignee_id
        ticket.assignee_id = assignee_id

        # Assignment drives status: taking a ticket moves it out of the inbox,
        # and unassigning returns it there.
        if assignee_id is None and ticket.status is TicketStatus.ASSIGNED:
            await self._set_status(ticket, TicketStatus.OPEN, actor, note="Unassigned")
        elif assignee_id is not None and ticket.status is TicketStatus.OPEN:
            await self._set_status(ticket, TicketStatus.ASSIGNED, actor, note="Assigned")

        ticket.version += 1
        await self._session.flush()

        await self._audit.record(
            tenant_id=self._tenant_id,
            actor_id=actor.user_id,
            action=AuditAction.TICKET_ASSIGNED,
            entity_type="ticket",
            entity_id=ticket.id,
            before={"assignee_id": str(previous) if previous else None},
            after={"assignee_id": str(assignee_id) if assignee_id else None},
        )
        await self._invalidate_dashboards()
        return ticket

    async def start(self, actor: Actor, ticket_id: uuid.UUID, version: int) -> Ticket:
        return await self._transition(
            actor, ticket_id, version, TicketStatus.IN_PROGRESS, AuditAction.TICKET_TRANSITIONED
        )

    async def hold(
        self,
        actor: Actor,
        ticket_id: uuid.UUID,
        version: int,
        waiting_on: WaitingOn,
        note: str | None,
    ) -> Ticket:
        ticket = await self._transition(
            actor,
            ticket_id,
            version,
            TicketStatus.ON_HOLD,
            AuditAction.TICKET_TRANSITIONED,
            note=note,
        )
        ticket.on_hold_waiting_on = waiting_on
        await self._session.flush()
        return ticket

    async def resolve(
        self, actor: Actor, ticket_id: uuid.UUID, version: int, resolution_notes: str
    ) -> Ticket:
        ticket = await self._transition(
            actor,
            ticket_id,
            version,
            TicketStatus.RESOLVED,
            AuditAction.TICKET_RESOLVED,
            note="Resolved",
        )
        ticket.resolution_notes = resolution_notes
        ticket.resolved_at = datetime.now(UTC)
        ticket.on_hold_waiting_on = None
        await self._session.flush()
        return ticket

    async def reject(
        self,
        actor: Actor,
        ticket_id: uuid.UUID,
        version: int,
        reason: RejectionReason,
        note: str,
    ) -> Ticket:
        ticket = await self._transition(
            actor,
            ticket_id,
            version,
            TicketStatus.REJECTED,
            AuditAction.TICKET_REJECTED,
            note=note,
        )
        ticket.rejection_reason = reason
        ticket.resolved_at = datetime.now(UTC)
        ticket.on_hold_waiting_on = None
        await self._session.flush()
        return ticket

    async def close(
        self, actor: Actor, ticket_id: uuid.UUID, version: int, note: str | None
    ) -> Ticket:
        """Only CS reaches here -- enforced by the transition table.

        A developer knows the code is fixed; only CS knows the client agrees it
        is fixed.
        """
        ticket = await self._transition(
            actor,
            ticket_id,
            version,
            TicketStatus.CLOSED,
            AuditAction.TICKET_CLOSED,
            note=note,
        )
        ticket.closed_at = datetime.now(UTC)
        await self._session.flush()
        await self._invalidate_dashboards()
        return ticket

    async def reopen(self, actor: Actor, ticket_id: uuid.UUID, version: int, reason: str) -> Ticket:
        ticket = await self._transition(
            actor,
            ticket_id,
            version,
            TicketStatus.ASSIGNED,
            AuditAction.TICKET_REOPENED,
            note=reason,
        )
        ticket.reopen_count += 1
        ticket.closed_at = None
        ticket.resolved_at = None
        ticket.resolution_notes = None
        ticket.rejection_reason = None
        await self._session.flush()
        await self._invalidate_dashboards()
        return ticket

    async def transfer_team(
        self, actor: Actor, ticket_id: uuid.UUID, version: int, team_id: uuid.UUID, reason: str
    ) -> Ticket:
        ticket = await self.get(ticket_id)
        self._assert_version(ticket, version)
        policies.assert_can_transfer_team(actor, ticket_facts(ticket))

        if team_id == ticket.team_id:
            raise ValidationFailedError("The ticket already belongs to that team.")

        previous_team = ticket.team_id
        ticket.team_id = team_id
        # Back to the new team's inbox: the previous assignee is not on it.
        ticket.assignee_id = None
        if ticket.status is not TicketStatus.OPEN:
            await self._set_status(ticket, TicketStatus.OPEN, actor, note=f"Transferred: {reason}")
        ticket.version += 1
        await self._session.flush()

        await self._audit.record(
            tenant_id=self._tenant_id,
            actor_id=actor.user_id,
            action=AuditAction.TICKET_TEAM_TRANSFERRED,
            entity_type="ticket",
            entity_id=ticket.id,
            before={"team_id": str(previous_team)},
            after={"team_id": str(team_id), "reason": reason},
        )
        await self._invalidate_dashboards()
        return ticket

    async def override_priority(
        self, actor: Actor, ticket_id: uuid.UUID, version: int, priority: Priority, reason: str
    ) -> Ticket:
        ticket = await self.get(ticket_id)
        self._assert_version(ticket, version)
        policies.assert_can_override_priority(actor, ticket_facts(ticket))

        if priority is ticket.priority:
            raise ValidationFailedError("The ticket already has that priority.")

        previous = ticket.priority
        ticket.priority = priority
        ticket.priority_overridden = True
        ticket.priority_override_reason = reason
        ticket.priority_overridden_by_id = actor.user_id
        ticket.version += 1
        await self._session.flush()

        # Recorded so override frequency can be measured. Frequent overrides
        # mean the severity/impact matrix needs tuning, not that people are
        # misbehaving.
        await self._audit.record(
            tenant_id=self._tenant_id,
            actor_id=actor.user_id,
            action=AuditAction.TICKET_PRIORITY_OVERRIDDEN,
            entity_type="ticket",
            entity_id=ticket.id,
            before={"priority": previous.value},
            after={"priority": priority.value, "reason": reason},
        )
        return ticket

    # ----------------------------------------------------------- helpers

    async def _transition(
        self,
        actor: Actor,
        ticket_id: uuid.UUID,
        version: int,
        to_status: TicketStatus,
        action: AuditAction,
        note: str | None = None,
    ) -> Ticket:
        ticket = await self.get(ticket_id)
        self._assert_version(ticket, version)

        facts = ticket_facts(ticket)
        policies.assert_transition_allowed(actor, facts, to_status)

        from_status = ticket.status
        await self._set_status(ticket, to_status, actor, note=note)
        ticket.version += 1
        await self._session.flush()

        await self._audit.record(
            tenant_id=self._tenant_id,
            actor_id=actor.user_id,
            action=action,
            entity_type="ticket",
            entity_id=ticket.id,
            before={"status": from_status.value},
            after={"status": to_status.value},
        )
        logger.info(
            "ticket transitioned",
            extra={
                "ticket_id": str(ticket.id),
                "from_status": from_status.value,
                "to_status": to_status.value,
            },
        )
        return ticket

    async def _set_status(
        self,
        ticket: Ticket,
        to_status: TicketStatus,
        actor: Actor,
        note: str | None = None,
    ) -> None:
        from_status = ticket.status
        ticket.status = to_status
        if to_status is not TicketStatus.ON_HOLD:
            ticket.on_hold_waiting_on = None
        await self._record_transition(ticket, from_status, to_status, actor.user_id, note)

    async def _record_transition(
        self,
        ticket: Ticket,
        from_status: TicketStatus | None,
        to_status: TicketStatus,
        changed_by: uuid.UUID,
        note: str | None = None,
    ) -> None:
        self._session.add(
            TicketStatusHistory(
                tenant_id=self._tenant_id,
                ticket_id=ticket.id,
                from_status=from_status,
                to_status=to_status,
                changed_by_id=changed_by,
                note=note,
            )
        )

    @staticmethod
    def _assert_version(ticket: Ticket, version: int) -> None:
        if ticket.version != version:
            raise VersionConflictError()

    async def _invalidate_dashboards(self) -> None:
        await self._cache.delete_prefix(CacheKey.tenant_prefix(self._tenant_id, "dashboard"))


def ticket_facts(ticket: Ticket) -> TicketFacts:
    return TicketFacts(
        id=ticket.id,
        status=ticket.status,
        team_id=ticket.team_id,
        assignee_id=ticket.assignee_id,
    )
