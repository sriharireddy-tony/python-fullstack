"""User administration.

The deactivation flow is the part worth attention: setting ``is_active = false``
hides the *user*, but does nothing to their tickets. Without bulk reassignment,
a departing engineer's open tickets stay assigned to an account nobody can act
as -- a silent data black hole that nobody notices until a client asks.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Annotated

from fastapi import APIRouter, Depends, Query, status
from pydantic import BaseModel, ConfigDict, EmailStr, Field
from sqlalchemy import ColumnElement, delete, func, select

from app.core.errors import NotFoundError, ValidationFailedError
from app.core.pagination import Page, PageParams
from app.core.permissions import Permission, Role
from app.core.security import hash_password
from app.modules.audit.models import AuditAction
from app.modules.audit.service import AuditService
from app.modules.identity.dependencies import RequestContext, ScopedDb, require
from app.modules.identity.models import RefreshToken, User, UserTeam
from app.modules.identity.repository import RefreshTokenRepository

router = APIRouter()

CanManage = Annotated[RequestContext, Depends(require(Permission.USER_MANAGE))]
CanRead = Annotated[RequestContext, Depends(require(Permission.TICKET_READ))]


class UserCreate(BaseModel):
    email: EmailStr
    full_name: str = Field(min_length=1, max_length=200)
    role: Role
    # Length only, matching the server rule elsewhere. Composition rules push
    # people towards predictable substitutions.
    password: str = Field(min_length=12, max_length=256)
    team_ids: list[uuid.UUID] = []


class UserUpdate(BaseModel):
    full_name: str | None = Field(default=None, min_length=1, max_length=200)
    role: Role | None = None
    team_ids: list[uuid.UUID] | None = None


class DeactivateRequest(BaseModel):
    #: Where this user's open tickets should go. Null returns them to their
    #: team inboxes as unassigned.
    reassign_tickets_to: uuid.UUID | None = None


class UserResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    email: str
    full_name: str
    role: Role
    is_active: bool
    team_ids: list[uuid.UUID] = []
    last_login_at: datetime | None = None
    created_at: datetime


class DeactivateResponse(BaseModel):
    user_id: uuid.UUID
    tickets_reassigned: int
    sessions_revoked: int


async def _team_ids(db: ScopedDb, user_id: uuid.UUID) -> list[uuid.UUID]:
    result = await db.execute(select(UserTeam.team_id).where(UserTeam.user_id == user_id))
    return list(result.scalars().all())


async def _to_response(db: ScopedDb, user: User) -> UserResponse:
    return UserResponse(
        id=user.id,
        email=user.email,
        full_name=user.full_name,
        role=user.role,
        is_active=user.is_active,
        team_ids=await _team_ids(db, user.id),
        last_login_at=user.last_login_at,
        created_at=user.created_at,
    )


@router.get("", response_model=Page[UserResponse], summary="List users")
async def list_users(
    ctx: CanRead,
    db: ScopedDb,
    page: Annotated[int, Query(ge=1)] = 1,
    page_size: Annotated[int, Query(ge=1, le=100)] = 50,
    active_only: Annotated[bool, Query()] = False,
    role: Annotated[Role | None, Query()] = None,
    team_id: Annotated[uuid.UUID | None, Query()] = None,
    q: Annotated[str | None, Query()] = None,
) -> Page[UserResponse]:
    conditions: list[ColumnElement[bool]] = [User.deleted_at.is_(None)]
    if active_only:
        conditions.append(User.is_active.is_(True))
    if role:
        conditions.append(User.role == role)
    if q:
        conditions.append(User.full_name.ilike(f"%{q}%"))

    stmt = select(User).where(*conditions)
    count_stmt = select(func.count()).select_from(User).where(*conditions)

    if team_id:
        stmt = stmt.join(UserTeam, UserTeam.user_id == User.id).where(UserTeam.team_id == team_id)
        count_stmt = count_stmt.join(UserTeam, UserTeam.user_id == User.id).where(
            UserTeam.team_id == team_id
        )

    params = PageParams(page=page, page_size=page_size)
    total = await db.scalar(count_stmt)
    result = await db.execute(
        stmt.order_by(User.full_name).offset(params.offset).limit(params.limit)
    )
    users = list(result.scalars().unique().all())
    return Page.create([await _to_response(db, u) for u in users], total or 0, params)


@router.post(
    "", response_model=UserResponse, status_code=status.HTTP_201_CREATED, summary="Create a user"
)
async def create_user(payload: UserCreate, ctx: CanManage, db: ScopedDb) -> UserResponse:
    # Email is globally unique, so this checks across tenants -- the one place
    # that is deliberate, and it never reveals which tenant holds the address.
    existing = await db.scalar(
        select(func.count())
        .select_from(User)
        .where(func.lower(User.email) == payload.email.lower(), User.deleted_at.is_(None))
    )
    if existing:
        raise ValidationFailedError("An account with that email already exists.")

    user = User(
        tenant_id=ctx.tenant,
        email=payload.email,
        full_name=payload.full_name.strip(),
        role=payload.role,
        password_hash=hash_password(payload.password),
        is_active=True,
        preferences={},
    )
    db.add(user)
    await db.flush()

    for index, team_id in enumerate(payload.team_ids):
        db.add(UserTeam(user_id=user.id, team_id=team_id, is_primary=index == 0))
    await db.flush()

    await AuditService(db).record(
        tenant_id=ctx.tenant,
        actor_id=ctx.user_id,
        action=AuditAction.USER_CREATED,
        entity_type="user",
        entity_id=user.id,
        after={"role": payload.role.value, "team_count": len(payload.team_ids)},
    )
    return await _to_response(db, user)


@router.patch("/{user_id}", response_model=UserResponse, summary="Update a user")
async def update_user(
    user_id: uuid.UUID, payload: UserUpdate, ctx: CanManage, db: ScopedDb
) -> UserResponse:
    user = await _load(db, user_id)
    before = {"role": user.role.value, "full_name": user.full_name}

    if payload.full_name is not None:
        user.full_name = payload.full_name.strip()
    if payload.role is not None:
        user.role = payload.role

    if payload.team_ids is not None:
        await db.execute(delete(UserTeam).where(UserTeam.user_id == user.id))
        for index, team_id in enumerate(payload.team_ids):
            db.add(UserTeam(user_id=user.id, team_id=team_id, is_primary=index == 0))

    await db.flush()

    await AuditService(db).record(
        tenant_id=ctx.tenant,
        actor_id=ctx.user_id,
        action=AuditAction.USER_UPDATED,
        entity_type="user",
        entity_id=user.id,
        before=before,
        after={"role": user.role.value, "full_name": user.full_name},
    )
    return await _to_response(db, user)


@router.post(
    "/{user_id}/deactivate",
    response_model=DeactivateResponse,
    summary="Deactivate a user and hand over their tickets",
)
async def deactivate_user(
    user_id: uuid.UUID, payload: DeactivateRequest, ctx: CanManage, db: ScopedDb
) -> DeactivateResponse:
    from app.modules.tickets.repository import TicketRepository

    user = await _load(db, user_id)

    if user.id == ctx.user_id:
        raise ValidationFailedError("You cannot deactivate your own account.")

    if payload.reassign_tickets_to:
        target = await _load(db, payload.reassign_tickets_to)
        if not target.is_active:
            raise ValidationFailedError("Tickets cannot be reassigned to an inactive user.")

    reassigned = await TicketRepository(db).reassign_all(user.id, payload.reassign_tickets_to)

    user.is_active = False
    # Existing sessions must end immediately -- otherwise a deactivated user
    # keeps working until their refresh token expires.
    revoked = await RefreshTokenRepository(db).revoke_all_for_user(user.id)
    await db.flush()

    audit = AuditService(db)
    await audit.record(
        tenant_id=ctx.tenant,
        actor_id=ctx.user_id,
        action=AuditAction.USER_DEACTIVATED,
        entity_type="user",
        entity_id=user.id,
        after={"sessions_revoked": revoked},
    )
    if reassigned:
        await audit.record(
            tenant_id=ctx.tenant,
            actor_id=ctx.user_id,
            action=AuditAction.USER_TICKETS_REASSIGNED,
            entity_type="user",
            entity_id=user.id,
            after={
                "count": reassigned,
                "to": str(payload.reassign_tickets_to) if payload.reassign_tickets_to else "inbox",
            },
        )

    return DeactivateResponse(
        user_id=user.id, tickets_reassigned=reassigned, sessions_revoked=revoked
    )


@router.post("/{user_id}/reactivate", response_model=UserResponse, summary="Reactivate a user")
async def reactivate_user(user_id: uuid.UUID, ctx: CanManage, db: ScopedDb) -> UserResponse:
    user = await _load(db, user_id)
    user.is_active = True
    await db.flush()
    await AuditService(db).record(
        tenant_id=ctx.tenant,
        actor_id=ctx.user_id,
        action=AuditAction.USER_REACTIVATED,
        entity_type="user",
        entity_id=user.id,
    )
    return await _to_response(db, user)


async def _load(db: ScopedDb, user_id: uuid.UUID) -> User:
    result = await db.execute(select(User).where(User.id == user_id, User.deleted_at.is_(None)))
    user = result.scalar_one_or_none()
    if user is None:
        raise NotFoundError("User not found.")
    return user


__all__ = ["UTC", "RefreshToken", "datetime", "router"]
