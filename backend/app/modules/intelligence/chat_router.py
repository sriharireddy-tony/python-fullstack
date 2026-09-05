"""Chat endpoints.

A separate router from the ticket-scoped AI endpoints because these are not
about a ticket. Mounted at ``/chat``.
"""

from __future__ import annotations

import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel, ConfigDict, Field

from app.core.config import settings
from app.core.errors import RateLimitError, ServiceUnavailableError
from app.core.logging import get_logger
from app.core.permissions import Permission
from app.modules.identity.dependencies import RequestContext, ScopedDb, require
from app.modules.intelligence.chat_service import ChatService
from app.modules.intelligence.deps import get_ai_deps
from app.modules.intelligence.guardrails.budget import check_user_rate

logger = get_logger(__name__)

router = APIRouter()

#: Chatting about tickets requires being able to read tickets. Nothing more:
#: the assistant has no write tools, so it can never do anything the caller
#: could not do by reading.
CanChat = Annotated[RequestContext, Depends(require(Permission.TICKET_READ))]


class ConversationOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    title: str | None
    message_count: int
    created_at: object
    last_message_at: object | None = None
    expires_at: object


class StartConversation(BaseModel):
    title: str | None = Field(default=None, max_length=200)


class SendMessage(BaseModel):
    message: str = Field(min_length=1, max_length=2000)


class ChatTurnOut(BaseModel):
    """One answered turn."""

    answer: str
    model: str | None = None
    tool_calls: int = 0
    #: Which nodes ran. Diagnostics, shown behind a toggle rather than inline.
    trace: list[str] = Field(default_factory=list)
    #: Set when an input guardrail refused the message.
    blocked_reason: str = ""
    #: False when the checkpointer is unavailable and the thread will forget on
    #: restart. Surfaced rather than hidden: an assistant with amnesia is worth
    #: warning someone about.
    persistent: bool = True


class ChatMessageOut(BaseModel):
    role: str
    content: str


@router.post("", response_model=ConversationOut, status_code=201, summary="Start a conversation")
async def start_conversation(
    payload: StartConversation,
    ctx: CanChat,
    db: ScopedDb,
) -> ConversationOut:
    if not settings.AI_CHAT_ENABLED:
        raise ServiceUnavailableError("The assistant is disabled.")
    conversation = await ChatService(db, ctx.tenant, ctx.user_id).start(payload.title)
    return ConversationOut.model_validate(conversation)


@router.get("", response_model=list[ConversationOut], summary="My conversations")
async def list_conversations(
    ctx: CanChat,
    db: ScopedDb,
    limit: Annotated[int, Query(ge=1, le=50)] = 30,
) -> list[ConversationOut]:
    """Only the caller's own threads.

    Private to their creator, not shared within the tenant. A question someone
    asks an assistant is closer to a search history than to a ticket comment.
    """
    conversations = await ChatService(db, ctx.tenant, ctx.user_id).list_mine(limit)
    return [ConversationOut.model_validate(item) for item in conversations]


@router.post(
    "/{conversation_id}/messages",
    response_model=ChatTurnOut,
    summary="Send a message and get the reply",
)
async def send_message(
    conversation_id: uuid.UUID,
    payload: SendMessage,
    ctx: CanChat,
    db: ScopedDb,
) -> ChatTurnOut:
    """Answer one message.

    Synchronous, unlike the analysis agent. A chat turn is one or two model
    calls and a couple of lookups — a few seconds, which is what someone
    typing a question expects. The agent's 202-and-poll design exists because
    a run takes a minute; applying it here would make a conversation feel
    broken.
    """
    if not settings.AI_CHAT_ENABLED:
        raise ServiceUnavailableError("The assistant is disabled.")

    budget = await check_user_rate(
        str(ctx.user_id),
        "chat",
        limit=settings.AI_CHAT_LIMIT_PER_HOUR,
        window_seconds=3600,
    )
    if not budget.allowed:
        raise RateLimitError(budget.reason)

    try:
        ai = await get_ai_deps()
    except Exception as exc:
        logger.warning("chat unavailable", extra={"error": type(exc).__name__})
        raise ServiceUnavailableError("The assistant is temporarily unavailable.") from exc

    result = await ChatService(db, ctx.tenant, ctx.user_id).send(
        conversation_id, payload.message, ai
    )
    return ChatTurnOut(**result)


@router.get(
    "/{conversation_id}/messages",
    response_model=list[ChatMessageOut],
    summary="The transcript",
)
async def conversation_history(
    conversation_id: uuid.UUID,
    ctx: CanChat,
    db: ScopedDb,
) -> list[ChatMessageOut]:
    """Read the transcript back from the checkpointer.

    The transcript is not stored in our own table: two copies of a
    conversation is two things to keep in step, and the checkpoint is the one
    the graph actually reads.
    """
    messages = await ChatService(db, ctx.tenant, ctx.user_id).history(conversation_id)
    return [ChatMessageOut(**message) for message in messages]


@router.delete("/{conversation_id}", status_code=204, summary="Delete a conversation")
async def delete_conversation(
    conversation_id: uuid.UUID,
    ctx: CanChat,
    db: ScopedDb,
) -> None:
    """Delete the thread and its checkpoints.

    Both halves. Deleting the row alone would leave checkpoint state with no
    owner and no retention policy.
    """
    await ChatService(db, ctx.tenant, ctx.user_id).delete(conversation_id)
