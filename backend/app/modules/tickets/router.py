"""Ticket endpoints.

Actions are endpoints, not a status field on a PATCH.

    Every action has a different permission, different required fields, and
    different side effects. A generic status PATCH collapses all of that into
    one branching function and makes the permission matrix impossible to
    express at the route level. With explicit endpoints each one declares its
    own dependency and validates its own payload -- and the OpenAPI schema
    documents exactly what can be done to a ticket.

Every ticket path accepts **either** a UUID or a human reference (``OS-1042``).
People quote the reference far more often than the id, and having some routes
accept it while others demand a UUID produces confusing 422s at exactly the
wrong moment.
"""

from __future__ import annotations

import uuid
from datetime import date
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Header, Query, Request

from app.core.pagination import Page, PageParams
from app.core.permissions import Permission
from app.core.throttle import idempotency
from app.modules.identity.dependencies import CurrentUser, RequestContext, ScopedDb, require
from app.modules.tickets.enums import (
    Environment,
    Priority,
    TicketStatus,
    derive_priority,
)
from app.modules.tickets.models import Ticket
from app.modules.tickets.policies import available_actions
from app.modules.tickets.repository import SORTABLE, TicketFilters
from app.modules.tickets.schemas import (
    AssignRequest,
    CloseRequest,
    HoldRequest,
    OverridePriorityRequest,
    PriorityPreviewRequest,
    PriorityPreviewResponse,
    RejectRequest,
    ReopenRequest,
    ResolveRequest,
    StartRequest,
    StatusHistoryEntry,
    TicketContentUpdate,
    TicketCreate,
    TicketDetail,
    TicketListItem,
    TransferTeamRequest,
)
from app.modules.tickets.service import RequestMeta, TicketService, ticket_facts
from app.modules.tickets.support import actor_from_context, to_detail, to_list_item

router = APIRouter()

CanCreate = Annotated[RequestContext, Depends(require(Permission.TICKET_CREATE))]
CanRead = Annotated[RequestContext, Depends(require(Permission.TICKET_READ))]
CanUpdate = Annotated[RequestContext, Depends(require(Permission.TICKET_UPDATE))]


def _meta(request: Request) -> RequestMeta:
    return RequestMeta(
        ip_address=request.client.host if request.client else None,
        user_agent=request.headers.get("user-agent"),
    )


def _looks_like_uuid(value: str) -> bool:
    try:
        uuid.UUID(value)
    except ValueError:
        return False
    return True


async def _resolve(service: TicketService, ticket_ref: str) -> Ticket:
    """Accept a UUID or an ``OS-1042`` reference. 404 for anything else."""
    if _looks_like_uuid(ticket_ref):
        return await service.get(uuid.UUID(ticket_ref))
    return await service.get_by_reference(ticket_ref)


# ------------------------------------------------------------------ reads


@router.get("", response_model=Page[TicketListItem], summary="List and search tickets")
async def list_tickets(
    ctx: CanRead,
    db: ScopedDb,
    page: Annotated[int, Query(ge=1)] = 1,
    page_size: Annotated[int, Query(ge=1, le=100)] = 25,
    status: Annotated[list[TicketStatus] | None, Query()] = None,
    priority: Annotated[list[Priority] | None, Query()] = None,
    team_id: Annotated[list[uuid.UUID] | None, Query()] = None,
    client_id: Annotated[list[uuid.UUID] | None, Query()] = None,
    assignee_id: Annotated[str | None, Query(description="A user id, or 'me'")] = None,
    unassigned: Annotated[bool, Query()] = False,
    awaiting_closure: Annotated[bool, Query()] = False,
    environment: Annotated[Environment | None, Query()] = None,
    created_from: Annotated[date | None, Query()] = None,
    created_to: Annotated[date | None, Query()] = None,
    q: Annotated[str | None, Query(description="Full-text search, or a ticket number")] = None,
    sort: Annotated[str | None, Query(description="Field, '-' prefix for descending")] = None,
) -> Page[TicketListItem]:
    # 'me' avoids a round trip to resolve the current user's id -- this is the
    # default dashboard view, so it is the hottest query in the app.
    resolved_assignee: uuid.UUID | None = None
    if assignee_id == "me":
        resolved_assignee = ctx.user_id
    elif assignee_id:
        resolved_assignee = uuid.UUID(assignee_id)

    filters = TicketFilters(
        statuses=status or [],
        priorities=priority or [],
        team_ids=team_id or [],
        client_ids=client_id or [],
        assignee_id=resolved_assignee,
        unassigned=unassigned,
        awaiting_closure=awaiting_closure,
        environment=environment,
        created_from=created_from,
        created_to=created_to,
        query=q,
    )

    raw = sort or "-created_at"
    descending = raw.startswith("-")
    field = raw.lstrip("-")
    if field not in SORTABLE:
        field, descending = "created_at", True

    service = TicketService(db, ctx.tenant)
    result = await service.search(
        filters, PageParams(page=page, page_size=page_size), sort_field=field, descending=descending
    )

    return Page[TicketListItem](
        items=[await to_list_item(db, t) for t in result.items],
        total=result.total,
        page=result.page,
        page_size=result.page_size,
    )


@router.post(
    "/priority-preview",
    response_model=PriorityPreviewResponse,
    summary="Preview the derived priority",
)
async def priority_preview(
    payload: PriorityPreviewRequest, _ctx: CanRead
) -> PriorityPreviewResponse:
    """Lets the create form show the priority as the user answers.

    Uses the same pure function the server uses on save, so the preview can
    never disagree with what actually gets stored.
    """
    priority = derive_priority(payload.severity, payload.impact, payload.workaround)
    modifier = {
        "none": " raised one level because there is no workaround",
        "easy": " lowered one level because an easy workaround exists",
        "painful": "",
    }[payload.workaround.value]
    return PriorityPreviewResponse(
        priority=priority,
        explanation=(
            f"{payload.severity.value.replace('_', ' ')} affecting "
            f"{payload.impact.value.replace('_', ' ')}{modifier}."
        ),
    )


@router.post("", response_model=TicketDetail, status_code=201, summary="Raise a ticket")
async def create_ticket(
    payload: TicketCreate,
    ctx: CanCreate,
    db: ScopedDb,
    request: Request,
    idempotency_key: Annotated[str | None, Header(alias="Idempotency-Key")] = None,
) -> TicketDetail:
    """Raise a ticket.

    Honours ``Idempotency-Key``: a retried or double-submitted request returns
    the ticket the first attempt created, rather than a duplicate. CS agents do
    double-click, and a duplicate ticket costs someone a cleanup later.
    """
    service = TicketService(db, ctx.tenant)
    actor = await actor_from_context(db, ctx)

    if idempotency_key:
        # Scoped by tenant so one tenant's key can never return another's
        # ticket, even if the keys collide.
        scope = f"ticket:create:{ctx.tenant}"
        existing_id = await idempotency.get(scope, idempotency_key)
        if existing_id:
            ticket = await service.get(uuid.UUID(existing_id))
            detail = await to_detail(db, ticket)
            detail.available_actions = available_actions(actor, ticket_facts(ticket))
            return detail

    ticket = await service.create(actor, payload, _meta(request))

    if idempotency_key:
        await idempotency.set(f"ticket:create:{ctx.tenant}", idempotency_key, str(ticket.id))

    detail = await to_detail(db, ticket)
    detail.available_actions = available_actions(actor, ticket_facts(ticket))
    return detail


@router.get("/{ticket_ref}", response_model=TicketDetail, summary="Get one ticket")
async def get_ticket(ticket_ref: str, ctx: CanRead, db: ScopedDb) -> TicketDetail:
    service = TicketService(db, ctx.tenant)
    ticket = await _resolve(service, ticket_ref)
    actor = await actor_from_context(db, ctx)
    detail = await to_detail(db, ticket)
    detail.available_actions = available_actions(actor, ticket_facts(ticket))
    return detail


@router.get(
    "/{ticket_ref}/history",
    response_model=list[StatusHistoryEntry],
    summary="Status transition history",
)
async def ticket_history(ticket_ref: str, ctx: CanRead, db: ScopedDb) -> list[StatusHistoryEntry]:
    service = TicketService(db, ctx.tenant)
    ticket = await _resolve(service, ticket_ref)
    entries = await service.history(ticket.id)
    return [StatusHistoryEntry.model_validate(e) for e in entries]


# ----------------------------------------------------------------- writes


@router.patch("/{ticket_ref}", response_model=TicketDetail, summary="Edit ticket content")
async def update_ticket(
    ticket_ref: str,
    payload: TicketContentUpdate,
    ctx: CanUpdate,
    db: ScopedDb,
    request: Request,
) -> TicketDetail:
    service = TicketService(db, ctx.tenant)
    actor = await actor_from_context(db, ctx)
    ticket = await _resolve(service, ticket_ref)
    ticket = await service.update_content(actor, ticket.id, payload, _meta(request))
    detail = await to_detail(db, ticket)
    detail.available_actions = available_actions(actor, ticket_facts(ticket))
    return detail


async def _run(
    db: ScopedDb,
    ctx: RequestContext,
    ticket_ref: str,
    handler_name: str,
    **kwargs: Any,
) -> TicketDetail:
    """Shared body for the nine action endpoints.

    Each route stays a thin declaration of its permission and payload; the
    resolve-act-serialise sequence is identical for all of them.
    """
    service = TicketService(db, ctx.tenant)
    actor = await actor_from_context(db, ctx)
    ticket = await _resolve(service, ticket_ref)

    handler = getattr(service, handler_name)
    ticket = await handler(actor, ticket.id, **kwargs)

    detail = await to_detail(db, ticket)
    detail.available_actions = available_actions(actor, ticket_facts(ticket))
    return detail


@router.post("/{ticket_ref}/assign", response_model=TicketDetail, summary="Assign or unassign")
async def assign(
    ticket_ref: str, payload: AssignRequest, ctx: CurrentUser, db: ScopedDb
) -> TicketDetail:
    return await _run(
        db, ctx, ticket_ref, "assign", version=payload.version, assignee_id=payload.assignee_id
    )


@router.post("/{ticket_ref}/start", response_model=TicketDetail, summary="Begin work")
async def start(
    ticket_ref: str, payload: StartRequest, ctx: CurrentUser, db: ScopedDb
) -> TicketDetail:
    return await _run(db, ctx, ticket_ref, "start", version=payload.version)


@router.post("/{ticket_ref}/hold", response_model=TicketDetail, summary="Put on hold")
async def hold(
    ticket_ref: str, payload: HoldRequest, ctx: CurrentUser, db: ScopedDb
) -> TicketDetail:
    return await _run(
        db,
        ctx,
        ticket_ref,
        "hold",
        version=payload.version,
        waiting_on=payload.waiting_on,
        note=payload.note,
    )


@router.post("/{ticket_ref}/resolve", response_model=TicketDetail, summary="Mark resolved")
async def resolve(
    ticket_ref: str, payload: ResolveRequest, ctx: CurrentUser, db: ScopedDb
) -> TicketDetail:
    return await _run(
        db,
        ctx,
        ticket_ref,
        "resolve",
        version=payload.version,
        resolution_notes=payload.resolution_notes,
    )


@router.post("/{ticket_ref}/reject", response_model=TicketDetail, summary="Reject")
async def reject(
    ticket_ref: str, payload: RejectRequest, ctx: CurrentUser, db: ScopedDb
) -> TicketDetail:
    return await _run(
        db,
        ctx,
        ticket_ref,
        "reject",
        version=payload.version,
        reason=payload.rejection_reason,
        note=payload.note,
    )


@router.post("/{ticket_ref}/close", response_model=TicketDetail, summary="Close (CS only)")
async def close(
    ticket_ref: str, payload: CloseRequest, ctx: CurrentUser, db: ScopedDb
) -> TicketDetail:
    return await _run(db, ctx, ticket_ref, "close", version=payload.version, note=payload.note)


@router.post("/{ticket_ref}/reopen", response_model=TicketDetail, summary="Reopen")
async def reopen(
    ticket_ref: str, payload: ReopenRequest, ctx: CurrentUser, db: ScopedDb
) -> TicketDetail:
    return await _run(db, ctx, ticket_ref, "reopen", version=payload.version, reason=payload.reason)


@router.post(
    "/{ticket_ref}/transfer-team", response_model=TicketDetail, summary="Move to another team"
)
async def transfer_team(
    ticket_ref: str, payload: TransferTeamRequest, ctx: CurrentUser, db: ScopedDb
) -> TicketDetail:
    return await _run(
        db,
        ctx,
        ticket_ref,
        "transfer_team",
        version=payload.version,
        team_id=payload.team_id,
        reason=payload.reason,
    )


@router.post(
    "/{ticket_ref}/override-priority", response_model=TicketDetail, summary="Override priority"
)
async def override_priority(
    ticket_ref: str, payload: OverridePriorityRequest, ctx: CurrentUser, db: ScopedDb
) -> TicketDetail:
    return await _run(
        db,
        ctx,
        ticket_ref,
        "override_priority",
        version=payload.version,
        priority=payload.priority,
        reason=payload.reason,
    )
