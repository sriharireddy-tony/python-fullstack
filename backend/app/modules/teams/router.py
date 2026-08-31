"""Team management.

Teams are admin-managed rows, not a hardcoded enum, so adding one is data entry
rather than a deployment. Storing them as rows is also what makes adding the
product-module dimension later a purely additive change.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Annotated

from fastapi import APIRouter, Depends, Query, status
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import func, select

from app.core.cache import Cache, CacheKey
from app.core.errors import NotFoundError, ValidationFailedError
from app.core.permissions import Permission
from app.modules.audit.models import AuditAction
from app.modules.audit.service import AuditService
from app.modules.identity.dependencies import RequestContext, ScopedDb, require
from app.modules.identity.models import User, UserTeam
from app.modules.teams.models import Team

router = APIRouter()

CanManage = Annotated[RequestContext, Depends(require(Permission.TEAM_MANAGE))]
CanRead = Annotated[RequestContext, Depends(require(Permission.TICKET_READ))]


class TeamCreate(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    code: str = Field(min_length=1, max_length=50)
    description: str | None = Field(default=None, max_length=500)
    manager_id: uuid.UUID | None = None


class TeamUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=120)
    description: str | None = Field(default=None, max_length=500)
    manager_id: uuid.UUID | None = None
    is_active: bool | None = None


class TeamResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    name: str
    code: str
    description: str | None = None
    manager_id: uuid.UUID | None = None
    is_active: bool
    member_count: int = 0
    created_at: datetime


@router.get("", response_model=list[TeamResponse], summary="List teams")
async def list_teams(
    ctx: CanRead,
    db: ScopedDb,
    active_only: Annotated[bool, Query()] = False,
) -> list[TeamResponse]:
    conditions = [Team.deleted_at.is_(None)]
    if active_only:
        conditions.append(Team.is_active.is_(True))

    result = await db.execute(select(Team).where(*conditions).order_by(Team.name))
    teams = list(result.scalars().all())

    # One grouped query rather than one per team.
    counts_result = await db.execute(
        select(UserTeam.team_id, func.count()).group_by(UserTeam.team_id)
    )
    counts: dict[uuid.UUID, int] = {row[0]: int(row[1]) for row in counts_result.all()}

    return [
        TeamResponse(**_fields(team), member_count=int(counts.get(team.id, 0))) for team in teams
    ]


def _fields(team: Team) -> dict[str, object]:
    return {
        "id": team.id,
        "name": team.name,
        "code": team.code,
        "description": team.description,
        "manager_id": team.manager_id,
        "is_active": team.is_active,
        "created_at": team.created_at,
    }


@router.post(
    "", response_model=TeamResponse, status_code=status.HTTP_201_CREATED, summary="Add a team"
)
async def create_team(payload: TeamCreate, ctx: CanManage, db: ScopedDb) -> TeamResponse:
    await _assert_unique(db, payload.name, payload.code)
    if payload.manager_id:
        await _assert_user_exists(db, payload.manager_id)

    team = Team(
        tenant_id=ctx.tenant,
        name=payload.name.strip(),
        code=payload.code.strip().upper(),
        description=payload.description,
        manager_id=payload.manager_id,
        is_active=True,
    )
    db.add(team)
    await db.flush()

    await AuditService(db).record(
        tenant_id=ctx.tenant,
        actor_id=ctx.user_id,
        action=AuditAction.TEAM_CREATED,
        entity_type="team",
        entity_id=team.id,
        after={"name": team.name, "code": team.code},
    )
    await Cache().delete(CacheKey.teams_list(ctx.tenant))
    return TeamResponse(**_fields(team))


@router.patch("/{team_id}", response_model=TeamResponse, summary="Update a team")
async def update_team(
    team_id: uuid.UUID, payload: TeamUpdate, ctx: CanManage, db: ScopedDb
) -> TeamResponse:
    team = await _load(db, team_id)
    before = {"name": team.name, "is_active": team.is_active}

    updates = payload.model_dump(exclude_unset=True)
    if updates.get("name"):
        await _assert_unique(db, updates["name"], team.code, exclude_id=team.id)
    if updates.get("manager_id"):
        await _assert_user_exists(db, updates["manager_id"])

    for key, value in updates.items():
        setattr(team, key, value)
    await db.flush()

    await AuditService(db).record(
        tenant_id=ctx.tenant,
        actor_id=ctx.user_id,
        action=AuditAction.TEAM_UPDATED,
        entity_type="team",
        entity_id=team.id,
        before=before,
        after={k: str(v) for k, v in updates.items()},
    )
    await Cache().delete(CacheKey.teams_list(ctx.tenant))
    return TeamResponse(**_fields(team))


@router.delete("/{team_id}", status_code=status.HTTP_204_NO_CONTENT, summary="Deactivate a team")
async def deactivate_team(team_id: uuid.UUID, ctx: CanManage, db: ScopedDb) -> None:
    from app.modules.tickets.enums import OPEN_STATUSES
    from app.modules.tickets.models import Ticket

    team = await _load(db, team_id)

    # A team with live work cannot simply vanish -- its tickets would become
    # unreachable from every inbox.
    open_tickets = await db.scalar(
        select(func.count())
        .select_from(Ticket)
        .where(
            Ticket.team_id == team_id,
            Ticket.deleted_at.is_(None),
            Ticket.status.in_(list(OPEN_STATUSES)),
        )
    )
    if open_tickets:
        raise ValidationFailedError(
            f"This team still has {open_tickets} open ticket(s). "
            "Transfer or close them before deactivating the team."
        )

    team.is_active = False
    team.deleted_at = datetime.now(UTC)
    await db.flush()
    await Cache().delete(CacheKey.teams_list(ctx.tenant))


async def _load(db: ScopedDb, team_id: uuid.UUID) -> Team:
    result = await db.execute(select(Team).where(Team.id == team_id, Team.deleted_at.is_(None)))
    team = result.scalar_one_or_none()
    if team is None:
        raise NotFoundError("Team not found.")
    return team


async def _assert_unique(
    db: ScopedDb, name: str, code: str, exclude_id: uuid.UUID | None = None
) -> None:
    conditions = [Team.deleted_at.is_(None), func.lower(Team.name) == name.strip().lower()]
    if exclude_id:
        conditions.append(Team.id != exclude_id)
    if await db.scalar(select(func.count()).select_from(Team).where(*conditions)):
        raise ValidationFailedError(f"A team named '{name}' already exists.")


async def _assert_user_exists(db: ScopedDb, user_id: uuid.UUID) -> None:
    user = await db.get(User, user_id)
    if user is None or user.deleted_at is not None:
        raise ValidationFailedError("The selected manager does not exist.")
