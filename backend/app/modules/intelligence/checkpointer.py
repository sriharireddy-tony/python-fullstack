"""The conversation checkpointer.

LangGraph persists graph state between turns through a *checkpointer*. This one
writes to Postgres, in its own schema, so a conversation survives a restart —
which is the whole difference between a chatbot and a stateless question box.

## Why this wraps the sync saver instead of using `AsyncPostgresSaver`

Because `AsyncPostgresSaver` cannot run here. It is built on psycopg's async
mode, and psycopg's async mode refuses to run on Windows' default event loop::

    InterfaceError: Psycopg cannot use the 'ProactorEventLoop' to run in
    async mode.

Forcing the whole application onto `SelectorEventLoop` to satisfy one component
would be the wrong trade — it is a global change with its own costs (a 512
socket ceiling, no subprocess support) to accommodate a component that writes
once per conversation turn.

The sync `PostgresSaver` works perfectly. Its async methods raise
`NotImplementedError`, so this class supplies them by delegating to the sync
ones in a worker thread. What that buys:

* **The official implementation does the hard part.** All the SQL, the
  serialisation format, and the schema migrations are LangGraph's, so there is
  no bespoke checkpoint format to keep compatible across upgrades.
* **One driver for the async path.** The application still talks asyncpg;
  psycopg is used only here, in a thread, where its sync mode is entirely at
  home.
* **The blocking calls are genuinely fine.** One round trip per turn, on a
  path that already waits seconds for a model. `asyncio.to_thread` keeps it off
  the event loop.

The alternative was implementing `BaseCheckpointSaver` from scratch over
SQLAlchemy — around two hundred lines covering checkpoints, blobs, and pending
writes, with the serialisation format as a permanent compatibility risk. Forty
lines of delegation is the better version of the same outcome.

## Why its own schema, and who creates it

`PostgresSaver.setup()` creates and owns its tables. Alembic's autogenerate
does not know about them and would propose dropping them — a migration that
silently deletes conversation history. So the tables live in their own schema,
`alembic/env.py` excludes that schema from comparison, and migration 0005
creates the schema with default privileges for the application role.

The schema is created by the **migration** role, not the application role:
``os_app`` owns nothing and cannot execute DDL, which is deliberate and worth
keeping. `setup()` therefore runs from `scripts/bootstrap_checkpointer.py` as
the migration role, once, like any other schema change.

## What it does not do

**It carries no tenant scoping.** Checkpoint rows are keyed by thread id and
read back by thread id, and the tables are outside our RLS policies because
they are not ours to police. So the tenant boundary for chat is enforced
*above* the checkpointer: a thread id only reaches the graph after the
``ai_conversations`` row has been loaded through a tenant-scoped session, and
that row is what proves the caller owns the thread. Anything that skipped that
lookup would skip the boundary — which is why `ChatService` is the only caller.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Sequence
from typing import Any

from langgraph.checkpoint.base import BaseCheckpointSaver

from app.core.config import settings
from app.core.logging import get_logger

logger = get_logger(__name__)

#: The checkpointer's own schema, excluded from Alembic. See the module
#: docstring.
CHECKPOINT_SCHEMA = "langgraph"

#: The tables `setup()` creates, for the bootstrap script to verify.
CHECKPOINT_TABLES = (
    "checkpoints",
    "checkpoint_blobs",
    "checkpoint_writes",
    "checkpoint_migrations",
)

_saver: Any | None = None
_lock = asyncio.Lock()


def psycopg_url(*, migration_role: bool = False) -> str:
    """The database URL in the form psycopg expects.

    The application's URL carries the ``+asyncpg`` driver marker, which psycopg
    does not understand. Rewritten here rather than adding a second URL
    setting, so there is one source of truth for where the database is and no
    way for two settings to drift apart and point at different databases.
    """
    url = settings.migration_database_url if migration_role else settings.DATABASE_URL
    for marker in ("+asyncpg", "+psycopg", "+psycopg2"):
        url = url.replace(marker, "")
    return url


class ThreadedPostgresSaver(BaseCheckpointSaver[str]):
    """Async checkpointer over LangGraph's sync `PostgresSaver`.

    **Subclasses `BaseCheckpointSaver` because it must.** The first version of
    this class did not: the graph appears to duck-type its checkpointer, so a
    plain wrapper looked sufficient. `compile()` disagreed::

        TypeError: Invalid checkpointer provided. Expected an instance of
        `BaseCheckpointSaver` ... Received ThreadedPostgresSaver.

    LangGraph runs an `isinstance` check, so structural compatibility is not
    enough. Worth remembering as a general point: "it has the right methods"
    is a guess about a library's contract, and the library is the authority.

    Inheriting turns out to cost nothing anyway — the sync methods delegate to
    the wrapped saver, so both halves of the interface work rather than half of
    it raising.
    """

    def __init__(self, saver: Any) -> None:
        # The base class holds the serialiser, and it has to be the wrapped
        # saver's own: a different serialiser would write checkpoints the sync
        # saver could not read back.
        super().__init__(serde=saver.serde)
        self._saver = saver

    @property
    def config_specs(self) -> Any:
        return self._saver.config_specs

    def get_next_version(self, current: Any, channel: Any = None) -> Any:
        """Version stamping. Pure computation, so no thread hop needed.

        Takes `channel` optionally because LangGraph has called this with one
        and two arguments across versions, and a signature mismatch here
        surfaces as an opaque failure deep inside a graph run.
        """
        try:
            return self._saver.get_next_version(current, channel)
        except TypeError:
            return self._saver.get_next_version(current)

    # --- sync passthrough -------------------------------------------------
    #
    # Delegated rather than left to raise, so the same object serves a sync
    # graph too. Nothing in this project uses one, but a half-implemented
    # interface is a trap for whoever does.

    def get_tuple(self, config: Any) -> Any:
        return self._saver.get_tuple(config)

    def list(self, config: Any, **kwargs: Any) -> Any:
        return self._saver.list(config, **kwargs)

    def put(self, config: Any, checkpoint: Any, metadata: Any, new_versions: Any) -> Any:
        return self._saver.put(config, checkpoint, metadata, new_versions)

    def put_writes(
        self, config: Any, writes: Sequence[tuple[str, Any]], task_id: str, task_path: str = ""
    ) -> None:
        self._saver.put_writes(config, writes, task_id, task_path)

    def delete_thread(self, thread_id: str) -> None:
        self._saver.delete_thread(thread_id)

    # --- reads ------------------------------------------------------------

    async def aget_tuple(self, config: Any) -> Any:
        return await asyncio.to_thread(self._saver.get_tuple, config)

    async def aget(self, config: Any) -> Any:
        return await asyncio.to_thread(self._saver.get, config)

    async def alist(
        self,
        config: Any,
        *,
        filter: dict[str, Any] | None = None,
        before: Any = None,
        limit: int | None = None,
    ) -> AsyncIterator[Any]:
        """List checkpoints.

        The sync `list` is a generator, so it is drained inside the thread
        rather than yielded across the boundary — iterating a sync generator
        from the event loop would run its queries on the loop, which is the one
        thing this wrapper exists to prevent.
        """

        def drain() -> list[Any]:
            return list(self._saver.list(config, filter=filter, before=before, limit=limit))

        for item in await asyncio.to_thread(drain):
            yield item

    # --- writes -----------------------------------------------------------

    async def aput(
        self,
        config: Any,
        checkpoint: Any,
        metadata: Any,
        new_versions: Any,
    ) -> Any:
        return await asyncio.to_thread(self._saver.put, config, checkpoint, metadata, new_versions)

    async def aput_writes(
        self,
        config: Any,
        writes: Sequence[tuple[str, Any]],
        task_id: str,
        task_path: str = "",
    ) -> None:
        await asyncio.to_thread(self._saver.put_writes, config, writes, task_id, task_path)

    async def adelete_thread(self, thread_id: str) -> None:
        """Delete one conversation's checkpoints.

        Needed by the retention job: the `ai_conversations` row is ours to
        delete, but the checkpoint rows are the saver's, and deleting the
        former without the latter leaves orphaned state that no longer has an
        owner or a retention policy.
        """
        await asyncio.to_thread(self._saver.delete_thread, thread_id)


async def get_checkpointer() -> Any:
    """The process-wide checkpointer.

    Returns ``None`` when the dependency or the database is unavailable, and
    the caller falls back to an in-memory checkpointer. That degradation is
    deliberate: a conversation that forgets on restart is much worse than one
    that remembers, and much better than a chat feature that will not start.
    """
    global _saver

    # One check, inside the lock, rather than the double-checked-locking
    # pattern. The unlocked fast path would save an uncontended `asyncio.Lock`
    # acquire -- which costs nothing measurable and happens once per chat turn
    # -- and it made the code untypeable: the checker narrows the global to
    # None after the outer check and then reports the inner re-check as
    # unreachable. Losing nothing to gain a type-checkable function is a good
    # trade.
    async with _lock:
        if _saver is not None:
            return _saver

        try:
            from langgraph.checkpoint.postgres import PostgresSaver
            from psycopg.rows import dict_row
            from psycopg_pool import ConnectionPool
        except ImportError:  # pragma: no cover - optional dependency
            logger.warning("langgraph-checkpoint-postgres is not installed; chat will not persist")
            return None

        def build() -> Any:
            # A *sync* pool, which is thread-safe and has no event-loop
            # constraints. Small on purpose: this serves one write per
            # conversation turn, and a large second pool against the same
            # database is a way to exhaust connections for no benefit.
            pool = ConnectionPool(
                conninfo=psycopg_url(),
                min_size=1,
                max_size=4,
                open=False,
                kwargs={
                    "autocommit": True,
                    "prepare_threshold": 0,
                    # `PostgresSaver` reads rows by column *name*, so a
                    # tuple row factory is not merely a typing mismatch --
                    # every read would fail. Caught by the type checker before
                    # it could be caught by a lost conversation.
                    "row_factory": dict_row,
                    # The saver issues unqualified table names, so the schema
                    # has to be on the search path for every connection the
                    # pool hands out -- not just the first.
                    "options": f"-c search_path={CHECKPOINT_SCHEMA}",
                },
            )
            pool.open(wait=True, timeout=10)
            with pool.connection() as conn:
                conn.execute("SELECT 1")
            return PostgresSaver(pool)  # type: ignore[arg-type]

        try:
            saver = await asyncio.to_thread(build)
        except Exception as exc:
            logger.warning(
                "checkpointer unavailable; chat will not persist across restarts",
                extra={"error": type(exc).__name__, "detail": str(exc)[:200]},
            )
            return None

        _saver = ThreadedPostgresSaver(saver)
        logger.info("conversation checkpointer ready", extra={"schema": CHECKPOINT_SCHEMA})
        return _saver
