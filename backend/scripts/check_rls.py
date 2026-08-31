"""Row-level-security coverage gate.

    cd backend
    .venv\\Scripts\\python -m scripts.check_rls

Exits non-zero if tenant isolation is incomplete. Intended for CI, and worth
running after any migration.

New tables get added under deadline pressure, and a missing RLS policy is
completely silent -- the application keeps working, the tests keep passing, and
one tenant can read another's data. This check is what makes that loud.
"""

from __future__ import annotations

import asyncio
import sys

from app.core.database import session_scope
from app.models_registry import TENANT_SCOPED_TABLES, check_rls_coverage
from sqlalchemy import text


async def main() -> None:
    problems: list[str] = []

    # 1. Model layer: every mapped table is classified, and scoped ones carry
    #    tenant_id. Needs no database.
    problems.extend(check_rls_coverage())

    # 2. Database layer: the policies actually exist, and are FORCED.
    async with session_scope(tenant_id=None) as db:
        rows = await db.execute(
            text(
                """
                SELECT c.relname,
                       c.relrowsecurity,
                       c.relforcerowsecurity,
                       (SELECT count(*) FROM pg_policies p
                         WHERE p.schemaname = 'public' AND p.tablename = c.relname) AS policies
                FROM pg_class c
                WHERE c.relnamespace = 'public'::regnamespace AND c.relkind = 'r'
                """
            )
        )
        state = {
            name: (enabled, forced, policies) for name, enabled, forced, policies in rows.all()
        }

    print(f"{'table':<26} {'RLS':<6} {'FORCED':<8} {'policies'}")
    print("-" * 52)
    for table in sorted(state):
        enabled, forced, policies = state[table]
        scoped = table in TENANT_SCOPED_TABLES
        marker = "*" if scoped else " "
        print(f"{marker}{table:<25} {enabled!s:<6} {forced!s:<8} {policies}")

    for table in sorted(TENANT_SCOPED_TABLES):
        if table not in state:
            problems.append(f"{table}: tenant-scoped but the table does not exist")
            continue
        enabled, forced, policies = state[table]
        if not enabled:
            problems.append(f"{table}: row-level security is NOT enabled")
        if not forced:
            # Without FORCE the table owner is exempt -- and the owner is the
            # role that runs migrations, so a maintenance script would silently
            # see every tenant at once.
            problems.append(f"{table}: RLS is not FORCED (the table owner would bypass it)")
        if policies == 0:
            problems.append(f"{table}: RLS enabled but no policy defined")

    # 3. The application role must never be able to skip RLS.
    async with session_scope(tenant_id=None) as db:
        bypass = await db.scalar(
            text("SELECT rolbypassrls FROM pg_roles WHERE rolname = current_user")
        )
        if bypass:
            problems.append(
                "the application connects as a role with BYPASSRLS - "
                "tenant isolation is not enforced at all"
            )

    print()
    if problems:
        print(f"{len(problems)} problem(s):")
        for problem in problems:
            print(f"  - {problem}")
        sys.exit(1)

    print(f"RLS coverage OK: {len(TENANT_SCOPED_TABLES)} tenant-scoped tables, all enforced.")


if __name__ == "__main__":
    asyncio.run(main())
