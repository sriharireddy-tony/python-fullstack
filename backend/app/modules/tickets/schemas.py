"""Ticket request and response models."""

from __future__ import annotations

import uuid
from datetime import datetime

from pydantic import BaseModel, ConfigDict, EmailStr, Field, computed_field

from app.modules.tickets.enums import (
    Environment,
    Impact,
    Priority,
    RejectionReason,
    Severity,
    TicketStatus,
    WaitingOn,
    Workaround,
)


class TicketCreate(BaseModel):
    """What CS fills in.

    Note what is absent: **priority**. CS answers severity, impact, and
    workaround -- observable facts -- and the system derives priority from them.
    """

    title: str = Field(min_length=3, max_length=300)
    description: str = Field(min_length=1)

    client_id: uuid.UUID
    team_id: uuid.UUID
    environment: Environment

    severity: Severity
    impact: Impact
    workaround: Workaround

    steps_to_reproduce: str | None = None
    expected_result: str | None = None
    actual_result: str | None = None

    reporter_name: str | None = Field(default=None, max_length=200)
    reporter_email: EmailStr | None = None

    #: Optional direct assignment. Left empty, the ticket waits in the team inbox.
    assignee_id: uuid.UUID | None = None


class TicketContentUpdate(BaseModel):
    """Content only -- never status. Status changes go through action endpoints.

    ``version`` is required: it is the optimistic-locking check that turns a
    concurrent edit into a clean 409 instead of a silent overwrite.
    """

    version: int

    title: str | None = Field(default=None, min_length=3, max_length=300)
    description: str | None = None
    steps_to_reproduce: str | None = None
    expected_result: str | None = None
    actual_result: str | None = None
    environment: Environment | None = None
    client_id: uuid.UUID | None = None
    reporter_name: str | None = Field(default=None, max_length=200)
    reporter_email: EmailStr | None = None
    ado_work_item_url: str | None = Field(default=None, max_length=2000)


class VersionedAction(BaseModel):
    version: int


class AssignRequest(VersionedAction):
    #: Null unassigns, returning the ticket to the team inbox.
    assignee_id: uuid.UUID | None = None


class HoldRequest(VersionedAction):
    waiting_on: WaitingOn
    note: str | None = None


class ResolveRequest(VersionedAction):
    resolution_notes: str = Field(min_length=1, description="What was done. Required.")


class RejectRequest(VersionedAction):
    rejection_reason: RejectionReason
    note: str = Field(min_length=1, description="Explain the rejection. Required.")


class CloseRequest(VersionedAction):
    note: str | None = None


class ReopenRequest(VersionedAction):
    reason: str = Field(min_length=1)


class TransferTeamRequest(VersionedAction):
    team_id: uuid.UUID
    reason: str = Field(min_length=1)


class OverridePriorityRequest(VersionedAction):
    priority: Priority
    #: Mandatory. Without it there is no way to tell whether the matrix is
    #: systematically wrong or whether the override was arbitrary.
    reason: str = Field(min_length=1)


class StartRequest(VersionedAction):
    pass


# ---------------------------------------------------------------- responses


class UserRef(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    full_name: str


class NamedRef(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    name: str


class TicketListItem(BaseModel):
    """The shape the ticket table renders.

    Deliberately excludes description and repro text: a fifty-row list should
    not carry paragraphs of HR support content over the wire.
    """

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    ticket_number: int
    title: str
    status: TicketStatus
    priority: Priority
    severity: Severity
    impact: Impact
    environment: Environment
    client: NamedRef | None = None
    team: NamedRef | None = None
    assignee: UserRef | None = None
    created_at: datetime
    updated_at: datetime
    reopen_count: int
    version: int

    @computed_field  # type: ignore[prop-decorator]
    @property
    def reference(self) -> str:
        return f"OS-{self.ticket_number}"


class TicketDetail(TicketListItem):
    description: str
    steps_to_reproduce: str | None = None
    expected_result: str | None = None
    actual_result: str | None = None
    workaround: Workaround
    reporter_name: str | None = None
    reporter_email: str | None = None
    created_by: UserRef | None = None
    resolution_notes: str | None = None
    rejection_reason: RejectionReason | None = None
    on_hold_waiting_on: WaitingOn | None = None
    ado_work_item_url: str | None = None
    priority_overridden: bool = False
    priority_override_reason: str | None = None
    resolved_at: datetime | None = None
    closed_at: datetime | None = None
    #: What the current user may do to this ticket right now. A UI convenience;
    #: every action is still authorised independently server-side.
    available_actions: list[str] = []


class StatusHistoryEntry(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    from_status: TicketStatus | None
    to_status: TicketStatus
    changed_by: UserRef | None = None
    note: str | None = None
    changed_at: datetime


class PriorityPreviewRequest(BaseModel):
    severity: Severity
    impact: Impact
    workaround: Workaround


class PriorityPreviewResponse(BaseModel):
    priority: Priority
    explanation: str
