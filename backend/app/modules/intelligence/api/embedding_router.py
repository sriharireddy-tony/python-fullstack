"""HTTP surface for the embedding console.

Every route requires ``EMBEDDING_MANAGE``, which team managers and tenant
admins hold and CS agents and developers do not. The permission is checked in
the dependency rather than in each handler, so a new route added here cannot
forget it.

Tenant isolation is the session's, not this file's: `get_scoped_db` binds the
request's tenant and row-level security does the rest, which is why no query
below mentions a tenant id.
"""

from __future__ import annotations

import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.permissions import Permission
from app.modules.identity.dependencies import RequestContext, ScopedDb, require
from app.modules.intelligence.deps import get_ai_deps
from app.modules.intelligence.embedding_console import (
    EmbeddingConsole,
    EmbeddingState,
)

router = APIRouter(prefix="/ai/embeddings", tags=["embeddings"])

CanManage = Annotated[RequestContext, Depends(require(Permission.EMBEDDING_MANAGE))]


# --------------------------------------------------------------------- schemas


class EmbeddingRowOut(BaseModel):
    ticket_id: uuid.UUID
    ticket_number: int
    title: str
    status: str
    priority: str
    team_id: uuid.UUID
    state: str
    model: str | None
    dimension: int | None
    embedded_at: str | None
    text_length: int


class EmbeddingListOut(BaseModel):
    rows: list[EmbeddingRowOut]
    total: int
    limit: int
    offset: int


class EmbeddingSummaryOut(BaseModel):
    total: int
    embedded: int
    stale: int
    not_embedded: int
    vectors_in_store: int
    model: str
    dimension: int | None
    #: False when automatic embedding on write is disabled, which is what makes
    #: the console the only way vectors get created. Surfaced so the page can
    #: say so rather than leaving an operator to wonder why nothing embedded
    #: itself.
    auto_embed_on_write: bool


class EmbedRequest(BaseModel):
    ticket_ids: list[uuid.UUID] = Field(min_length=1, max_length=100)
    #: Re-embed even when the text is unchanged. Without this, embedding an
    #: already-current ticket is a no-op.
    force: bool = False


class EmbedOutcomeOut(BaseModel):
    requested: int
    embedded: int
    skipped: int
    failed: int
    duration_seconds: float
    errors: list[str]


class DeleteRequest(BaseModel):
    ticket_ids: list[uuid.UUID] = Field(min_length=1, max_length=500)


class DeleteOutcomeOut(BaseModel):
    deleted: int


# ---------------------------------------------------------------------- routes


async def _console(ctx: RequestContext, db: AsyncSession) -> EmbeddingConsole:
    ai = await get_ai_deps()
    return EmbeddingConsole(db, ctx.tenant, ai.embeddings, ai.store)


@router.get("", response_model=EmbeddingListOut, summary="List tickets and their index status")
async def list_embeddings(
    ctx: CanManage,
    db: ScopedDb,
    team_id: uuid.UUID | None = None,
    ticket_status: str | None = Query(default=None, alias="status"),
    state: EmbeddingState | None = None,
    search: str | None = None,
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
) -> EmbeddingListOut:
    console = await _console(ctx, db)
    rows, total = await console.list_rows(
        team_id=team_id,
        status=ticket_status,
        state=state,
        search=search,
        limit=limit,
        offset=offset,
    )
    return EmbeddingListOut(
        rows=[
            EmbeddingRowOut(
                ticket_id=r.ticket_id,
                ticket_number=r.ticket_number,
                title=r.title,
                status=r.status,
                priority=r.priority,
                team_id=r.team_id,
                state=r.state.value,
                model=r.model,
                dimension=r.dimension,
                embedded_at=r.embedded_at.isoformat() if r.embedded_at else None,
                text_length=r.text_length,
            )
            for r in rows
        ],
        total=total,
        limit=limit,
        offset=offset,
    )


@router.get("/summary", response_model=EmbeddingSummaryOut, summary="Index totals")
async def embedding_summary(
    ctx: CanManage,
    db: ScopedDb,
) -> EmbeddingSummaryOut:
    console = await _console(ctx, db)
    s = await console.summary()
    return EmbeddingSummaryOut(
        total=s.total,
        embedded=s.embedded,
        stale=s.stale,
        not_embedded=s.not_embedded,
        vectors_in_store=s.vectors_in_store,
        model=s.model,
        dimension=s.dimension,
        auto_embed_on_write=settings.AI_AUTO_EMBED_ON_WRITE,
    )


@router.post("/embed", response_model=EmbedOutcomeOut, summary="Embed the selected tickets")
async def embed_tickets(
    payload: EmbedRequest,
    ctx: CanManage,
    db: ScopedDb,
) -> EmbedOutcomeOut:
    """Embed now, synchronously, and report what happened.

    Synchronous because a person is waiting and wants an answer, not a job id.
    The batch cap lives in the console service and is reported as a 400 rather
    than silently truncating -- an operator who asked for more than the cap
    must not believe the remainder succeeded.
    """
    console = await _console(ctx, db)
    try:
        outcome = await console.embed(payload.ticket_ids, force=payload.force)
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc

    return EmbedOutcomeOut(
        requested=outcome.requested,
        embedded=outcome.embedded,
        skipped=outcome.skipped,
        failed=outcome.failed,
        duration_seconds=outcome.duration_seconds,
        errors=outcome.errors,
    )


@router.post("/delete", response_model=DeleteOutcomeOut, summary="Delete the selected embeddings")
async def delete_embeddings(
    payload: DeleteRequest,
    ctx: CanManage,
    db: ScopedDb,
) -> DeleteOutcomeOut:
    """Remove vectors. The tickets themselves are untouched.

    A POST rather than DELETE because the body carries the id list, and DELETE
    with a request body is inconsistently supported across proxies and clients.
    """
    console = await _console(ctx, db)
    return DeleteOutcomeOut(deleted=await console.delete(payload.ticket_ids))
