"""Client management.

Client selection is mandatory on every ticket, which is exactly why clients are
managed records rather than a free-text field: otherwise "Acme", "acme corp",
and "ACME Ltd" all appear and every client filter and report degrades.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Annotated

from fastapi import APIRouter, Depends, Query, status
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import func, select

from app.core.cache import TTL, Cache, CacheKey
from app.core.errors import NotFoundError, ValidationFailedError
from app.core.pagination import Page, PageParams
from app.core.permissions import Permission
from app.modules.audit.models import AuditAction
from app.modules.audit.service import AuditService
from app.modules.clients.models import Client, ClientTier
from app.modules.identity.dependencies import RequestContext, ScopedDb, require

router = APIRouter()

CanManage = Annotated[RequestContext, Depends(require(Permission.CLIENT_MANAGE))]
CanRead = Annotated[RequestContext, Depends(require(Permission.TICKET_READ))]


class ClientCreate(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    code: str = Field(min_length=1, max_length=50)
    tier: ClientTier = ClientTier.STANDARD
    notes: str | None = None


class ClientUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=200)
    code: str | None = Field(default=None, min_length=1, max_length=50)
    tier: ClientTier | None = None
    notes: str | None = None
    is_active: bool | None = None


class ClientResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    name: str
    code: str
    tier: ClientTier
    notes: str | None = None
    is_active: bool
    created_at: datetime


@router.get("", response_model=Page[ClientResponse], summary="List clients")
async def list_clients(
    ctx: CanRead,
    db: ScopedDb,
    page: Annotated[int, Query(ge=1)] = 1,
    page_size: Annotated[int, Query(ge=1, le=100)] = 50,
    active_only: Annotated[bool, Query()] = False,
    q: Annotated[str | None, Query()] = None,
) -> Page[ClientResponse]:
    conditions = [Client.deleted_at.is_(None)]
    if active_only:
        conditions.append(Client.is_active.is_(True))
    if q:
        conditions.append(Client.name.ilike(f"%{q}%"))

    params = PageParams(page=page, page_size=page_size)
    total = await db.scalar(select(func.count()).select_from(Client).where(*conditions))
    result = await db.execute(
        select(Client)
        .where(*conditions)
        .order_by(Client.name)
        .offset(params.offset)
        .limit(params.limit)
    )
    return Page.create(
        [ClientResponse.model_validate(c) for c in result.scalars().all()], total or 0, params
    )


@router.post(
    "", response_model=ClientResponse, status_code=status.HTTP_201_CREATED, summary="Add a client"
)
async def create_client(payload: ClientCreate, ctx: CanManage, db: ScopedDb) -> ClientResponse:
    await _assert_unique(db, payload.name, payload.code)

    client = Client(
        tenant_id=ctx.tenant,
        name=payload.name.strip(),
        code=payload.code.strip().upper(),
        tier=payload.tier,
        notes=payload.notes,
        is_active=True,
    )
    db.add(client)
    await db.flush()

    await AuditService(db).record(
        tenant_id=ctx.tenant,
        actor_id=ctx.user_id,
        action=AuditAction.CLIENT_CREATED,
        entity_type="client",
        entity_id=client.id,
        after={"name": client.name, "code": client.code, "tier": client.tier.value},
    )
    await _invalidate(ctx.tenant)
    return ClientResponse.model_validate(client)


@router.patch("/{client_id}", response_model=ClientResponse, summary="Update a client")
async def update_client(
    client_id: uuid.UUID, payload: ClientUpdate, ctx: CanManage, db: ScopedDb
) -> ClientResponse:
    client = await _load(db, client_id)
    before = {
        "name": client.name,
        "code": client.code,
        "tier": client.tier.value,
        "is_active": client.is_active,
    }

    updates = payload.model_dump(exclude_unset=True)
    if "name" in updates or "code" in updates:
        await _assert_unique(
            db,
            updates.get("name", client.name),
            updates.get("code", client.code),
            exclude_id=client.id,
        )

    for key, value in updates.items():
        if value is not None:
            setattr(client, key, value)
    await db.flush()

    await AuditService(db).record(
        tenant_id=ctx.tenant,
        actor_id=ctx.user_id,
        action=AuditAction.CLIENT_UPDATED,
        entity_type="client",
        entity_id=client.id,
        before=before,
        after={k: str(v) for k, v in updates.items()},
    )
    await _invalidate(ctx.tenant)
    return ClientResponse.model_validate(client)


@router.delete(
    "/{client_id}", status_code=status.HTTP_204_NO_CONTENT, summary="Deactivate a client"
)
async def deactivate_client(client_id: uuid.UUID, ctx: CanManage, db: ScopedDb) -> None:
    """Soft delete.

    Tickets reference clients with ON DELETE RESTRICT, so a hard delete would
    fail anyway — and losing the client name would make historical tickets
    unreadable.
    """
    client = await _load(db, client_id)
    client.is_active = False
    client.deleted_at = datetime.now(UTC)
    await db.flush()

    await AuditService(db).record(
        tenant_id=ctx.tenant,
        actor_id=ctx.user_id,
        action=AuditAction.CLIENT_UPDATED,
        entity_type="client",
        entity_id=client.id,
        after={"deleted": True},
    )
    await _invalidate(ctx.tenant)


async def _load(db: ScopedDb, client_id: uuid.UUID) -> Client:
    result = await db.execute(
        select(Client).where(Client.id == client_id, Client.deleted_at.is_(None))
    )
    client = result.scalar_one_or_none()
    if client is None:
        raise NotFoundError("Client not found.")
    return client


async def _assert_unique(
    db: ScopedDb, name: str, code: str, exclude_id: uuid.UUID | None = None
) -> None:
    """Checked in the application for a readable error.

    The partial unique indexes on the table are the real guarantee — this check
    races, and losing the race surfaces as a 409 from the database rather than
    a corrupt row.
    """
    conditions = [
        Client.deleted_at.is_(None),
        func.lower(Client.name) == name.strip().lower(),
    ]
    if exclude_id:
        conditions.append(Client.id != exclude_id)
    if await db.scalar(select(func.count()).select_from(Client).where(*conditions)):
        raise ValidationFailedError(f"A client named '{name}' already exists.")

    conditions = [
        Client.deleted_at.is_(None),
        func.lower(Client.code) == code.strip().lower(),
    ]
    if exclude_id:
        conditions.append(Client.id != exclude_id)
    if await db.scalar(select(func.count()).select_from(Client).where(*conditions)):
        raise ValidationFailedError(f"A client with code '{code}' already exists.")


async def _invalidate(tenant_id: uuid.UUID) -> None:
    await Cache().delete(CacheKey.clients_list(tenant_id))


__all__ = ["TTL", "router"]
