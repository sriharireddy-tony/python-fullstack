"""Ticket conversation endpoints."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Annotated

from fastapi import APIRouter, Depends, status
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import desc, select

from app.core.errors import NotFoundError, PermissionDeniedError
from app.core.permissions import Permission
from app.modules.audit.models import AuditAction
from app.modules.audit.service import AuditService
from app.modules.comments.models import Comment
from app.modules.identity.dependencies import RequestContext, ScopedDb, require
from app.modules.identity.models import User
from app.modules.tickets import policies
from app.modules.tickets.service import TicketService, ticket_facts
from app.modules.tickets.support import actor_from_context

router = APIRouter()

CanComment = Annotated[RequestContext, Depends(require(Permission.COMMENT_CREATE))]
CanRead = Annotated[RequestContext, Depends(require(Permission.TICKET_READ))]

#: An author may correct a typo, but not silently rewrite history after others
#: have replied. Fifteen minutes is enough for the former and not the latter.
EDIT_WINDOW_MINUTES = 15


class CommentCreate(BaseModel):
    body: str = Field(min_length=1, max_length=20000)


class CommentUpdate(BaseModel):
    body: str = Field(min_length=1, max_length=20000)


class AuthorRef(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    full_name: str


class CommentResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    ticket_id: uuid.UUID
    body: str
    author: AuthorRef | None = None
    created_at: datetime
    edited_at: datetime | None = None
    can_edit: bool = False


async def _to_response(db: ScopedDb, comment: Comment, viewer_id: uuid.UUID) -> CommentResponse:
    author = await db.get(User, comment.author_id)
    editable = comment.author_id == viewer_id and _within_edit_window(comment)
    return CommentResponse(
        id=comment.id,
        ticket_id=comment.ticket_id,
        body=comment.body,
        author=AuthorRef(id=author.id, full_name=author.full_name) if author else None,
        created_at=comment.created_at,
        edited_at=comment.edited_at,
        can_edit=editable,
    )


def _within_edit_window(comment: Comment) -> bool:
    age = datetime.now(UTC) - comment.created_at
    return age.total_seconds() < EDIT_WINDOW_MINUTES * 60


@router.get(
    "/tickets/{ticket_id}/comments",
    response_model=list[CommentResponse],
    summary="Ticket conversation",
)
async def list_comments(ticket_id: uuid.UUID, ctx: CanRead, db: ScopedDb) -> list[CommentResponse]:
    # 404 before exposing anything, including for another tenant's ticket.
    await TicketService(db, ctx.tenant).get(ticket_id)

    result = await db.execute(
        select(Comment)
        .where(Comment.ticket_id == ticket_id, Comment.deleted_at.is_(None))
        .order_by(Comment.created_at)
    )
    return [await _to_response(db, c, ctx.user_id) for c in result.scalars().all()]


@router.post(
    "/tickets/{ticket_id}/comments",
    response_model=CommentResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Add a comment",
)
async def create_comment(
    ticket_id: uuid.UUID, payload: CommentCreate, ctx: CanComment, db: ScopedDb
) -> CommentResponse:
    ticket = await TicketService(db, ctx.tenant).get(ticket_id)
    actor = await actor_from_context(db, ctx)
    policies.assert_can_comment(actor, ticket_facts(ticket))

    comment = Comment(
        tenant_id=ctx.tenant,
        ticket_id=ticket_id,
        author_id=ctx.user_id,
        body=payload.body,
    )
    db.add(comment)
    await db.flush()

    # The body is deliberately not copied into the audit payload -- it may
    # contain payroll figures or personal data, and the audit log is exposed
    # in the UI.
    await AuditService(db).record(
        tenant_id=ctx.tenant,
        actor_id=ctx.user_id,
        action=AuditAction.COMMENT_CREATED,
        entity_type="comment",
        entity_id=comment.id,
        after={"ticket_id": str(ticket_id)},
    )
    return await _to_response(db, comment, ctx.user_id)


@router.patch(
    "/comments/{comment_id}", response_model=CommentResponse, summary="Edit your own comment"
)
async def update_comment(
    comment_id: uuid.UUID, payload: CommentUpdate, ctx: CanComment, db: ScopedDb
) -> CommentResponse:
    comment = await _load(db, comment_id)

    if comment.author_id != ctx.user_id:
        raise PermissionDeniedError("You can only edit your own comments.")
    if not _within_edit_window(comment):
        raise PermissionDeniedError(
            f"Comments can only be edited within {EDIT_WINDOW_MINUTES} minutes of posting."
        )

    comment.body = payload.body
    comment.edited_at = datetime.now(UTC)
    await db.flush()
    return await _to_response(db, comment, ctx.user_id)


@router.delete(
    "/comments/{comment_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Delete your own comment",
)
async def delete_comment(comment_id: uuid.UUID, ctx: CanComment, db: ScopedDb) -> None:
    comment = await _load(db, comment_id)

    is_author = comment.author_id == ctx.user_id
    is_admin = ctx.has(Permission.USER_MANAGE)
    if not (is_author or is_admin):
        raise PermissionDeniedError("You can only delete your own comments.")

    # Soft delete: the conversation is evidence for how a rejection was agreed.
    comment.deleted_at = datetime.now(UTC)
    await db.flush()

    await AuditService(db).record(
        tenant_id=ctx.tenant,
        actor_id=ctx.user_id,
        action=AuditAction.COMMENT_DELETED,
        entity_type="comment",
        entity_id=comment.id,
    )


async def _load(db: ScopedDb, comment_id: uuid.UUID) -> Comment:
    result = await db.execute(
        select(Comment).where(Comment.id == comment_id, Comment.deleted_at.is_(None))
    )
    comment = result.scalar_one_or_none()
    if comment is None:
        raise NotFoundError("Comment not found.")
    return comment


__all__ = ["desc", "router"]
