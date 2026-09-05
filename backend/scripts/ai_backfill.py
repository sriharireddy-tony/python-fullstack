"""Embed existing tickets, and reconcile Postgres against Pinecone.

    .venv\\Scripts\\python -m scripts.ai_backfill              # embed what is missing
    .venv\\Scripts\\python -m scripts.ai_backfill --reconcile   # report and fix drift
    .venv\\Scripts\\python -m scripts.ai_backfill --status      # just report

Runs the embedding inline rather than through the queue, because a backfill
wants to finish now and report, not trickle through a worker. It uses the same
idempotent path, so re-running is nearly free.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
import time
import uuid

from app.core.config import settings
from app.core.database import session_scope
from app.core.logging import configure_logging
from app.modules.intelligence.deps import get_ai_deps
from app.modules.intelligence.embeddings.service import EmbeddingService
from app.modules.intelligence.repository import VectorStateRepository
from app.modules.tenancy.models import Tenant
from app.modules.tickets.models import Ticket
from sqlalchemy import select

CHUNK = 16


async def tenants() -> list[tuple[uuid.UUID, str]]:
    async with session_scope(tenant_id=None) as db:
        rows = await db.execute(select(Tenant.id, Tenant.name).where(Tenant.deleted_at.is_(None)))
        return [(row[0], row[1]) for row in rows.all()]


async def backfill(tenant_id: uuid.UUID, name: str) -> None:
    deps = await get_ai_deps()
    started = time.perf_counter()

    async with session_scope(tenant_id=tenant_id) as db:
        rows = await db.execute(
            select(Ticket).where(Ticket.deleted_at.is_(None)).order_by(Ticket.ticket_number)
        )
        tickets = list(rows.scalars().all())

    if not tickets:
        print(f"  {name}: no tickets")
        return

    print(f"  {name}: {len(tickets)} tickets, embedding in chunks of {CHUNK}")
    embedded = 0
    for start in range(0, len(tickets), CHUNK):
        chunk = tickets[start : start + CHUNK]
        # A transaction per chunk, so an interruption keeps the work already
        # done rather than rolling back the entire backfill.
        async with session_scope(tenant_id=tenant_id) as db:
            service = EmbeddingService(db, deps.embeddings, deps.store)
            embedded += await service.embed_tickets(chunk)
        done = min(start + CHUNK, len(tickets))
        print(f"    {done}/{len(tickets)}  (embedded {embedded}, skipped {done - embedded})")

    elapsed = time.perf_counter() - started
    rate = embedded / elapsed if elapsed and embedded else 0.0
    print(f"  {name}: embedded {embedded} in {elapsed:.1f}s ({rate:.1f}/s)")


async def reconcile(tenant_id: uuid.UUID, name: str, fix: bool) -> dict[str, int]:
    """Compare Postgres against Pinecone and report drift.

    Two stores means the vectors can diverge from the tickets — the price of
    not using pgvector. Three discrepancies are possible, and each has a
    different fix.
    """
    deps = await get_ai_deps()
    model = deps.embeddings.model_name

    async with session_scope(tenant_id=tenant_id) as db:
        rows = await db.execute(select(Ticket.id).where(Ticket.deleted_at.is_(None)))
        live_tickets = set(rows.scalars().all())
        recorded = await VectorStateRepository(db).embedded_ids(model)

    in_store = await deps.store.list_ids(tenant_id)

    missing = live_tickets - recorded  # tickets never embedded
    orphaned = in_store - live_tickets  # vectors whose ticket is gone
    unrecorded = in_store - recorded  # in Pinecone, no bookkeeping row
    lost = recorded - in_store  # bookkeeping says yes, Pinecone says no

    print(f"  {name}")
    print(f"    tickets            {len(live_tickets)}")
    print(f"    vector_state rows  {len(recorded)}")
    print(f"    ids in store       {len(in_store)}")
    print(f"    missing vectors    {len(missing)}")
    print(f"    orphaned vectors   {len(orphaned)}")
    print(f"    unrecorded in pg   {len(unrecorded)}")
    print(f"    lost from store   {len(lost)}")

    if fix:
        if orphaned:
            await deps.store.delete(tenant_id, list(orphaned))
            print(f"    deleted {len(orphaned)} orphaned vectors")
        if lost:
            # The bookkeeping is wrong, not the vector — clear it so the
            # normal idempotent path re-embeds on the next pass.
            async with session_scope(tenant_id=tenant_id) as db:
                await VectorStateRepository(db).forget(list(lost))
            print(f"    cleared {len(lost)} stale state rows for re-embedding")
        if missing or lost:
            await backfill(tenant_id, name)

    return {
        "tickets": len(live_tickets),
        "missing": len(missing),
        "orphaned": len(orphaned),
        "lost": len(lost),
    }


async def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--reconcile", action="store_true", help="report and fix drift")
    parser.add_argument("--status", action="store_true", help="report only, change nothing")
    args = parser.parse_args()

    deps = await get_ai_deps()
    print("embedding model :", deps.embeddings.model_name)
    print("dimension       :", deps.embeddings.dimension)
    print(
        "pinecone        :",
        f"index={settings.PINECONE_INDEX} ({settings.PINECONE_CLOUD}/{settings.PINECONE_REGION})",
    )
    if not await deps.store.health():
        print("\nPinecone is not reachable. Start it with:", file=sys.stderr)
        print("  check PINECONE_API_KEY in backend/.env", file=sys.stderr)
        raise SystemExit(1)
    print()

    found = await tenants()
    if not found:
        print("no tenants")
        return

    problems = 0
    for tenant_id, name in found:
        if args.status:
            result = await reconcile(tenant_id, name, fix=False)
            problems += result["missing"] + result["orphaned"] + result["lost"]
        elif args.reconcile:
            result = await reconcile(tenant_id, name, fix=True)
        else:
            await backfill(tenant_id, name)

    if args.status and problems:
        print(f"\n{problems} discrepancies. Run with --reconcile to fix.")
        raise SystemExit(1)
    print("\ndone")


if __name__ == "__main__":
    configure_logging()
    asyncio.run(main())
