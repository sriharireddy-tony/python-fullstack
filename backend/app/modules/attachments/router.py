"""Attachment upload and download.

Downloads stream through a permission-checked endpoint. The upload directory is
**never** served statically -- this system holds HR support data, and a
guessable URL to a payroll screenshot is exactly the failure to avoid.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Annotated

from fastapi import APIRouter, Depends, File, UploadFile, status
from fastapi.responses import Response
from pydantic import BaseModel, ConfigDict
from sqlalchemy import func, select

from app.core.config import settings
from app.core.errors import NotFoundError, PermissionDeniedError, ValidationFailedError
from app.core.logging import get_logger
from app.core.permissions import Permission
from app.modules.attachments.models import Attachment
from app.modules.attachments.storage import (
    build_storage_key,
    checksum,
    get_storage,
    safe_display_name,
    validate_upload,
)
from app.modules.audit.models import AuditAction
from app.modules.audit.service import AuditService
from app.modules.comments.models import Comment
from app.modules.identity.dependencies import RequestContext, ScopedDb, require
from app.modules.tickets.service import TicketService

logger = get_logger(__name__)
router = APIRouter()

CanUpload = Annotated[RequestContext, Depends(require(Permission.ATTACHMENT_UPLOAD))]
CanRead = Annotated[RequestContext, Depends(require(Permission.TICKET_READ))]


class AttachmentResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    original_filename: str
    content_type: str
    size_bytes: int
    uploaded_by_id: uuid.UUID
    created_at: datetime
    ticket_id: uuid.UUID | None = None
    comment_id: uuid.UUID | None = None


async def _store(
    db: ScopedDb,
    ctx: RequestContext,
    upload: UploadFile,
    *,
    ticket_id: uuid.UUID | None,
    comment_id: uuid.UUID | None,
) -> Attachment:
    data = await upload.read()
    filename = upload.filename or "file"
    content_type = upload.content_type or "application/octet-stream"

    # Extension, declared type, size, and magic bytes are all checked. None of
    # them is trustworthy alone: all four come from the client.
    validate_upload(filename, content_type, data)

    key = build_storage_key(ctx.tenant, filename)
    await get_storage().save(key, data, content_type)

    attachment = Attachment(
        tenant_id=ctx.tenant,
        ticket_id=ticket_id,
        comment_id=comment_id,
        uploaded_by_id=ctx.user_id,
        original_filename=safe_display_name(filename),
        storage_key=key,
        content_type=content_type,
        size_bytes=len(data),
        checksum=checksum(data),
    )
    db.add(attachment)
    await db.flush()

    await AuditService(db).record(
        tenant_id=ctx.tenant,
        actor_id=ctx.user_id,
        action=AuditAction.ATTACHMENT_UPLOADED,
        entity_type="attachment",
        entity_id=attachment.id,
        after={"size_bytes": len(data), "content_type": content_type},
    )
    return attachment


@router.post(
    "/tickets/{ticket_id}/attachments",
    response_model=AttachmentResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Attach a file to a ticket",
)
async def upload_to_ticket(
    ticket_id: uuid.UUID,
    ctx: CanUpload,
    db: ScopedDb,
    file: Annotated[UploadFile, File()],
) -> AttachmentResponse:
    await TicketService(db, ctx.tenant).get(ticket_id)

    existing = await db.scalar(
        select(func.count())
        .select_from(Attachment)
        .where(Attachment.ticket_id == ticket_id, Attachment.deleted_at.is_(None))
    )
    if (existing or 0) >= settings.MAX_FILES_PER_TICKET:
        raise ValidationFailedError(
            f"A ticket can have at most {settings.MAX_FILES_PER_TICKET} attachments."
        )

    attachment = await _store(db, ctx, file, ticket_id=ticket_id, comment_id=None)
    return AttachmentResponse.model_validate(attachment)


@router.post(
    "/comments/{comment_id}/attachments",
    response_model=AttachmentResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Attach a file to a comment",
)
async def upload_to_comment(
    comment_id: uuid.UUID,
    ctx: CanUpload,
    db: ScopedDb,
    file: Annotated[UploadFile, File()],
) -> AttachmentResponse:
    comment = await db.get(Comment, comment_id)
    if comment is None or comment.deleted_at is not None:
        raise NotFoundError("Comment not found.")

    attachment = await _store(db, ctx, file, ticket_id=None, comment_id=comment_id)
    return AttachmentResponse.model_validate(attachment)


@router.get(
    "/tickets/{ticket_id}/attachments",
    response_model=list[AttachmentResponse],
    summary="List a ticket's attachments",
)
async def list_for_ticket(
    ticket_id: uuid.UUID, ctx: CanRead, db: ScopedDb
) -> list[AttachmentResponse]:
    await TicketService(db, ctx.tenant).get(ticket_id)
    result = await db.execute(
        select(Attachment)
        .where(Attachment.ticket_id == ticket_id, Attachment.deleted_at.is_(None))
        .order_by(Attachment.created_at)
    )
    return [AttachmentResponse.model_validate(a) for a in result.scalars().all()]


@router.get("/attachments/{attachment_id}/download", summary="Download an attachment")
async def download(attachment_id: uuid.UUID, ctx: CanRead, db: ScopedDb) -> Response:
    attachment = await _load(db, attachment_id)

    storage = get_storage()
    # On Azure Blob this becomes a short-lived SAS redirect: same route, same
    # permission check, different implementation behind the interface.
    direct = await storage.signed_url(attachment.storage_key, ttl_seconds=300)
    if direct:
        return Response(status_code=307, headers={"Location": direct})

    try:
        data = await storage.read(attachment.storage_key)
    except FileNotFoundError:
        logger.error("attachment missing from storage", extra={"attachment_id": str(attachment_id)})
        raise NotFoundError("The file is no longer available.") from None

    await AuditService(db).record(
        tenant_id=ctx.tenant,
        actor_id=ctx.user_id,
        action=AuditAction.ATTACHMENT_DOWNLOADED,
        entity_type="attachment",
        entity_id=attachment.id,
    )

    return Response(
        content=data,
        media_type=attachment.content_type,
        headers={
            # attachment, not inline: an uploaded HTML or SVG file must never
            # execute in the application's own origin.
            "Content-Disposition": f'attachment; filename="{attachment.original_filename}"',
            "X-Content-Type-Options": "nosniff",
            "Cache-Control": "private, no-store",
        },
    )


@router.delete(
    "/attachments/{attachment_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Delete an attachment",
)
async def delete_attachment(attachment_id: uuid.UUID, ctx: CanUpload, db: ScopedDb) -> None:
    attachment = await _load(db, attachment_id)

    is_uploader = attachment.uploaded_by_id == ctx.user_id
    if not (is_uploader or ctx.has(Permission.USER_MANAGE)):
        raise PermissionDeniedError("You can only delete files you uploaded.")

    # Soft delete only. The bytes stay until a retention job removes them --
    # deleting immediately would make an accidental delete unrecoverable, and
    # the retention policy is still an open decision.
    attachment.deleted_at = datetime.now(UTC)
    await db.flush()

    await AuditService(db).record(
        tenant_id=ctx.tenant,
        actor_id=ctx.user_id,
        action=AuditAction.ATTACHMENT_DELETED,
        entity_type="attachment",
        entity_id=attachment.id,
    )


async def _load(db: ScopedDb, attachment_id: uuid.UUID) -> Attachment:
    result = await db.execute(
        select(Attachment).where(Attachment.id == attachment_id, Attachment.deleted_at.is_(None))
    )
    attachment = result.scalar_one_or_none()
    if attachment is None:
        raise NotFoundError("Attachment not found.")
    return attachment
