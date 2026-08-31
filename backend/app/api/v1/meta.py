"""Enum metadata and audit access.

``/meta/enums`` means the frontend never hardcodes a dropdown. Add a rejection
reason on the backend and it appears in the UI with no frontend deployment --
and the labels stay in one place instead of drifting between the two codebases.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from enum import StrEnum
from typing import Annotated

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel, ConfigDict

from app.core.pagination import Page, PageParams
from app.core.permissions import Permission, Role
from app.modules.audit.service import AuditService
from app.modules.clients.models import ClientTier
from app.modules.identity.dependencies import CurrentUser, RequestContext, ScopedDb, require
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

router = APIRouter()

CanReadAudit = Annotated[RequestContext, Depends(require(Permission.AUDIT_READ))]


class EnumOption(BaseModel):
    value: str
    label: str
    #: Ordering hint so the UI does not have to know domain ordering.
    order: int


def _options(enum_cls: type[StrEnum], labels: dict[str, str]) -> list[EnumOption]:
    return [
        EnumOption(
            value=member.value,
            label=labels.get(member.value, member.value),
            order=index,
        )
        for index, member in enumerate(enum_cls)
    ]


SEVERITY_LABELS = {
    "s1_critical": "S1 — Critical (unusable, data loss, payroll blocked)",
    "s2_major": "S2 — Major (key function broken)",
    "s3_minor": "S3 — Minor (degraded)",
    "s4_cosmetic": "S4 — Cosmetic (no functional impact)",
}

IMPACT_LABELS = {
    "whole_org": "The whole organisation",
    "department": "A department",
    "few_users": "A few users",
    "single_user": "One user",
}

WORKAROUND_LABELS = {
    "none": "No workaround",
    "painful": "Workaround exists but is painful",
    "easy": "Easy workaround",
}

STATUS_LABELS = {
    "open": "Open",
    "assigned": "Assigned",
    "in_progress": "In Progress",
    "on_hold": "On Hold",
    "resolved": "Resolved",
    "rejected": "Rejected",
    "closed": "Closed",
}

PRIORITY_LABELS = {"p1": "P1", "p2": "P2", "p3": "P3", "p4": "P4"}

ENVIRONMENT_LABELS = {"production": "Production", "staging": "Staging", "uat": "UAT"}

REJECTION_LABELS = {
    "not_a_bug": "Not a bug",
    "duplicate": "Duplicate",
    "wont_fix": "Will not fix",
    "cannot_reproduce": "Cannot reproduce",
}

WAITING_ON_LABELS = {
    "cs": "Waiting on CS",
    "client": "Waiting on the client",
    "other_team": "Waiting on another team",
}

TIER_LABELS = {"standard": "Standard", "premium": "Premium", "enterprise": "Enterprise"}

ROLE_LABELS = {
    "tenant_admin": "Tenant Admin",
    "cs_lead": "CS Lead",
    "cs_agent": "CS Agent",
    "team_manager": "Team Manager",
    "developer": "Developer",
}


class EnumsResponse(BaseModel):
    severities: list[EnumOption]
    impacts: list[EnumOption]
    workarounds: list[EnumOption]
    priorities: list[EnumOption]
    statuses: list[EnumOption]
    environments: list[EnumOption]
    rejection_reasons: list[EnumOption]
    waiting_on: list[EnumOption]
    client_tiers: list[EnumOption]
    roles: list[EnumOption]


@router.get("/meta/enums", response_model=EnumsResponse, summary="All dropdown options")
async def enums(_ctx: CurrentUser) -> EnumsResponse:
    return EnumsResponse(
        severities=_options(Severity, SEVERITY_LABELS),
        impacts=_options(Impact, IMPACT_LABELS),
        workarounds=_options(Workaround, WORKAROUND_LABELS),
        priorities=_options(Priority, PRIORITY_LABELS),
        statuses=_options(TicketStatus, STATUS_LABELS),
        environments=_options(Environment, ENVIRONMENT_LABELS),
        rejection_reasons=_options(RejectionReason, REJECTION_LABELS),
        waiting_on=_options(WaitingOn, WAITING_ON_LABELS),
        client_tiers=_options(ClientTier, TIER_LABELS),
        roles=_options(Role, ROLE_LABELS),
    )


# ------------------------------------------------------------------ audit


class AuditEntry(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    actor_id: uuid.UUID | None
    actor_type: str
    action: str
    entity_type: str
    entity_id: uuid.UUID | None
    changes: dict[str, object]
    created_at: datetime


@router.get("/audit-logs", response_model=Page[AuditEntry], summary="Audit log")
async def audit_logs(
    ctx: CanReadAudit,
    db: ScopedDb,
    page: Annotated[int, Query(ge=1)] = 1,
    page_size: Annotated[int, Query(ge=1, le=100)] = 50,
    entity_type: Annotated[str | None, Query()] = None,
    entity_id: Annotated[uuid.UUID | None, Query()] = None,
    action: Annotated[str | None, Query()] = None,
) -> Page[AuditEntry]:
    params = PageParams(page=page, page_size=page_size)
    result = await AuditService(db).list_entries(
        tenant_id=ctx.tenant,
        params=params,
        entity_type=entity_type,
        entity_id=entity_id,
        action=action,
    )
    return Page.create([AuditEntry.model_validate(e) for e in result.items], result.total, params)


# --------------------------------------------------------------- dashboard


class DashboardCounts(BaseModel):
    my_open: int
    team_inbox: int
    awaiting_closure: int
    unassigned: int


@router.get(
    "/dashboard/counts", response_model=DashboardCounts, summary="Counts for tiles and badges"
)
async def dashboard_counts(ctx: CurrentUser, db: ScopedDb) -> DashboardCounts:
    from sqlalchemy import select

    from app.modules.identity.models import UserTeam
    from app.modules.tickets.service import TicketService

    teams = await db.execute(select(UserTeam.team_id).where(UserTeam.user_id == ctx.user_id))
    counts = await TicketService(db, ctx.tenant).dashboard_counts(
        user_id=ctx.user_id, team_ids=list(teams.scalars().all())
    )
    return DashboardCounts(**counts)
