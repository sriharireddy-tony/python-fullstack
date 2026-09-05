"""Conversations: creating them, answering in them, expiring them.

## Where the tenant boundary actually is

The checkpointer stores state by thread id and has no notion of a tenant — its
tables are LangGraph's, outside our row-level security. So the boundary is
enforced *here*, and the rule is exact:

> A thread id only reaches the graph after the `ai_conversations` row has been
> loaded through a tenant-scoped session, in the same request, for the calling
> user.

That row is the proof of ownership. Loading it is what RLS checks. Any path
that skipped the lookup and passed a thread id straight to the graph would skip
the boundary, which is why this service is the only caller of
`build_chat_graph` and why `_load_owned` exists as a single choke point.

Conversations are **private to their creator**, not shared within a tenant. A
question someone asks an assistant is closer to a search history than to a
ticket comment, and the surprise of a colleague reading it is worse than the
inconvenience of not being able to share it.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.errors import NotFoundError, PermissionDeniedError
from app.core.logging import get_logger
from app.modules.identity.models import User
from app.modules.intelligence.deps import AiDeps
from app.modules.intelligence.llm.registry import ModelRegistry
from app.modules.intelligence.models import Conversation
from app.modules.intelligence.pipelines.chat import ChatDeps, ChatState, build_chat_graph
from app.modules.intelligence.pipelines.checkpointer import get_checkpointer
from app.modules.intelligence.tools.registry import build_tools
from app.modules.intelligence.tools.tickets import ToolContext

logger = get_logger(__name__)

#: How many people's names are offered to the model for disambiguation.
#:
#: Bounded because it goes in a prompt. Enough to name the candidates when a
#: first name is ambiguous, which is the case that matters -- the seed has two
#: people called Divya, and "assign it to Divya" has two answers.
MAX_KNOWN_PEOPLE = 40


class ChatService:
    def __init__(self, session: AsyncSession, tenant_id: uuid.UUID, user_id: uuid.UUID) -> None:
        self._session = session
        self._tenant_id = tenant_id
        self._user_id = user_id

    # ------------------------------------------------------------ threads

    async def start(self, title: str | None = None) -> Conversation:
        """Open a new thread.

        `thread_id` is set to the row's own id rather than a separate random
        value. The column exists because the checkpointer's key is its own
        concept and should be nameable, but there is no reason for the two to
        differ -- and if they did, a mismatched pair would point a conversation
        at someone else's checkpoint. Same value, two names, one source of
        truth.
        """
        conversation = Conversation(
            tenant_id=self._tenant_id,
            user_id=self._user_id,
            title=(title or "New conversation")[:200],
            expires_at=datetime.now(UTC) + timedelta(days=settings.CHAT_RETENTION_DAYS),
            # Filled in after the flush assigns the id.
            thread_id="",
        )
        self._session.add(conversation)
        await self._session.flush()
        conversation.thread_id = str(conversation.id)
        await self._session.flush()
        return conversation

    async def list_mine(self, limit: int = 30) -> list[Conversation]:
        rows = await self._session.execute(
            select(Conversation)
            .where(
                Conversation.user_id == self._user_id,
                Conversation.deleted_at.is_(None),
            )
            .order_by(Conversation.updated_at.desc())
            .limit(limit)
        )
        return list(rows.scalars().all())

    async def _load_owned(self, conversation_id: uuid.UUID) -> Conversation:
        """Load a thread, proving the caller owns it.

        The single choke point for the tenant and user boundary. RLS scopes the
        query to the tenant; the explicit creator check scopes it to the
        person. Both, because they answer different questions and a tenant
        admin is not entitled to read a colleague's conversation.
        """
        conversation = await self._session.get(Conversation, conversation_id)
        if conversation is None or conversation.deleted_at is not None:
            raise NotFoundError("Conversation not found.")
        if conversation.user_id != self._user_id:
            # Deliberately a 403 rather than a 404. RLS has already proven the
            # row is in the caller's tenant, so its existence is not a secret
            # -- and pretending otherwise would make a genuine bug (a broken
            # link between colleagues) indistinguishable from a permission
            # problem.
            raise PermissionDeniedError("This conversation belongs to someone else.")
        return conversation

    async def delete(self, conversation_id: uuid.UUID) -> None:
        """Delete a thread and its checkpoints.

        Both halves, because the row is ours and the checkpoints are the
        saver's. Deleting only the row leaves state with no owner and no
        retention policy -- which is exactly the kind of orphan that turns a
        deletion request into an awkward conversation.
        """
        conversation = await self._load_owned(conversation_id)
        conversation.deleted_at = datetime.now(UTC)
        await self._session.flush()
        await _forget_checkpoints(conversation_id)

    # ------------------------------------------------------------ answering

    async def send(
        self,
        conversation_id: uuid.UUID,
        message: str,
        ai: AiDeps,
    ) -> dict[str, Any]:
        """Answer one message in a thread.

        The prior state is not passed in: LangGraph loads it from the
        checkpointer by thread id. All this supplies is the new message, which
        is what makes the conversation survive a restart -- there is nothing in
        this process that has to still be alive.
        """
        conversation = await self._load_owned(conversation_id)

        checkpointer = await get_checkpointer()
        if checkpointer is None:
            # In-memory, so the feature still works but forgets on restart.
            # Reported in the response rather than hidden: a user whose
            # assistant has amnesia deserves to know why.
            from langgraph.checkpoint.memory import InMemorySaver

            checkpointer = InMemorySaver()
            persistent = False
        else:
            persistent = True

        graph = build_chat_graph(checkpointer)
        deps = ChatDeps(
            registry=ModelRegistry(self._session, self._tenant_id),
            tools={
                tool.name: tool
                for tool in build_tools(
                    self._session,
                    ToolContext(tenant_id=self._tenant_id, user_id=self._user_id),
                    ai,
                )
            },
            known_people=await self._known_people(),
        )

        state: ChatState = {
            "tenant_id": self._tenant_id,
            "user_id": self._user_id,
            "conversation_id": conversation_id,
            "messages": [{"role": "user", "content": message}],
            # Reset per turn. Observations are evidence for one question, and
            # carrying them forward would grow every prompt for the life of the
            # thread.
            "observations": [],
            "call_signatures": [],
            "tool_calls": 0,
        }

        final = await graph.ainvoke(
            state,
            context=deps,
            # The thread id is what ties this turn to the stored state. It is
            # the conversation's own id, which was just proven to belong to the
            # caller.
            config={"configurable": {"thread_id": conversation.thread_id}},
        )

        answer = str(final.get("answer", "")).strip()

        conversation.last_message_at = datetime.now(UTC)
        conversation.message_count = conversation.message_count + 2
        if conversation.title == "New conversation":
            # Title from the first question, so the thread list is readable
            # without opening every thread. Truncated at a word boundary where
            # one is nearby, because a title cut mid-word looks broken.
            conversation.title = _title_from(message)
        # Every message extends the retention window: an active conversation
        # should not expire out from under someone mid-thread.
        conversation.expires_at = datetime.now(UTC) + timedelta(days=settings.CHAT_RETENTION_DAYS)
        await self._session.flush()

        logger.info(
            "chat turn complete",
            extra={
                "conversation_id": str(conversation_id),
                "model": final.get("model", "none"),
                "tool_calls": final.get("tool_calls", 0),
                "persistent": persistent,
            },
        )

        return {
            "answer": answer,
            "trace": final.get("trace", []),
            "model": final.get("model"),
            "tool_calls": final.get("tool_calls", 0),
            "blocked_reason": final.get("blocked_reason", ""),
            "persistent": persistent,
        }

    async def history(self, conversation_id: uuid.UUID) -> list[dict[str, str]]:
        """The transcript, read back from the checkpointer.

        Read from the checkpoint rather than stored in our own table on
        purpose: two copies of a conversation is two things to keep in step,
        and the checkpoint is the one the graph actually uses. Our table holds
        the *thread* -- who owns it, when it expires -- and not its contents.
        """
        # Ownership is proven before anything is read, exactly as in `send`.
        conversation = await self._load_owned(conversation_id)

        checkpointer = await get_checkpointer()
        if checkpointer is None:
            return []

        config = {"configurable": {"thread_id": conversation.thread_id}}
        snapshot = await checkpointer.aget(config)
        if not snapshot:
            return []

        messages = snapshot.get("channel_values", {}).get("messages", [])
        out: list[dict[str, str]] = []
        for message in messages:
            role = getattr(message, "type", None) or (
                message.get("role") if isinstance(message, dict) else None
            )
            content = getattr(message, "content", None) or (
                message.get("content") if isinstance(message, dict) else ""
            )
            if not content:
                continue
            out.append(
                {
                    "role": "user" if role in {"user", "human"} else "assistant",
                    "content": str(content),
                }
            )
        return out

    async def _known_people(self) -> tuple[str, ...]:
        """Names in this workspace, for disambiguation.

        Supplied to the model so an ambiguous first name produces a question
        naming the real candidates rather than a guess. The seed contains two
        people called Divya precisely so this case is exercised -- an assistant
        that silently picks one is worse than one that asks, because the reader
        cannot tell it chose.
        """
        rows = await self._session.execute(
            select(User.full_name)
            .where(User.is_active.is_(True), User.deleted_at.is_(None))
            .order_by(User.full_name)
            .limit(MAX_KNOWN_PEOPLE)
        )
        return tuple(name for (name,) in rows.all())

    # ------------------------------------------------------------ retention

    async def expire_due(self) -> int:
        """Delete conversations past their retention date.

        Monthly retention was an explicit requirement. Implemented as a sweep
        rather than a TTL because the checkpoint rows have to go too, and only
        this code knows which thread ids they belong to.
        """
        now = datetime.now(UTC)
        rows = await self._session.execute(
            select(Conversation.id).where(
                Conversation.expires_at.is_not(None),
                Conversation.expires_at < now,
                Conversation.deleted_at.is_(None),
            )
        )
        due = list(rows.scalars().all())
        if not due:
            return 0

        await self._session.execute(delete(Conversation).where(Conversation.id.in_(due)))
        for conversation_id in due:
            await _forget_checkpoints(conversation_id)

        logger.info("expired conversations", extra={"count": len(due)})
        return len(due)

    async def counts(self) -> dict[str, int]:
        total = await self._session.scalar(
            select(func.count()).select_from(Conversation).where(Conversation.deleted_at.is_(None))
        )
        return {"conversations": int(total or 0)}


async def _forget_checkpoints(conversation_id: uuid.UUID) -> None:
    """Remove a thread's checkpoints, if the saver supports it.

    Guarded rather than assumed: the in-memory fallback has no
    `adelete_thread`, and a retention job that crashes on a missing method
    would leave every later conversation unexpired.
    """
    checkpointer = await get_checkpointer()
    if checkpointer is None:
        return
    delete_thread = getattr(checkpointer, "adelete_thread", None)
    if delete_thread is None:
        return
    try:
        await delete_thread(str(conversation_id))
    except Exception as exc:
        logger.warning(
            "could not delete checkpoints for a conversation",
            extra={"conversation_id": str(conversation_id), "error": type(exc).__name__},
        )


def _title_from(message: str) -> str:
    """A thread title from its first question."""
    cleaned = " ".join(message.split())
    if len(cleaned) <= 60:
        return cleaned
    cut = cleaned[:60]
    space = cut.rfind(" ")
    return (cut[:space] if space > 40 else cut) + "…"
