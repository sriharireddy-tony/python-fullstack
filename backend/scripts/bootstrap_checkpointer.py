r"""Create the LangGraph checkpoint schema and tables.

    .venv\Scripts\python -m scripts.bootstrap_checkpointer

Run once, and again after upgrading LangGraph -- `setup()` applies the
checkpointer's own migrations, so a version bump may add or alter tables.

## Why this is a script and not application startup

Because it is DDL, and the application role does not do DDL. `os_app` owns
nothing and cannot create a schema or a table; that is a deliberate property of
this project's security model, and having the chat feature quietly need
CREATE privileges would undo it.

So the schema is created by migration 0005 and the tables by this script, both
as the migration role -- the same way every other schema change happens. The
running application only ever reads and writes rows.
"""

from __future__ import annotations

import asyncio

from app.core.logging import configure_logging, get_logger
from app.modules.intelligence.checkpointer import (
    CHECKPOINT_SCHEMA,
    CHECKPOINT_TABLES,
    psycopg_url,
)

logger = get_logger(__name__)


def main() -> None:
    configure_logging()

    import psycopg
    from langgraph.checkpoint.postgres import PostgresSaver
    from psycopg.rows import dict_row

    # The migration role, because this creates tables.
    url = psycopg_url(migration_role=True)
    # `dict_row` because the saver reads rows by column name.
    with psycopg.connect(url, autocommit=True, row_factory=dict_row) as conn:
        conn.execute(f"CREATE SCHEMA IF NOT EXISTS {CHECKPOINT_SCHEMA}")
        conn.execute(f"SET search_path TO {CHECKPOINT_SCHEMA}")

        PostgresSaver(conn).setup()

        rows = conn.execute(
            "SELECT tablename FROM pg_tables WHERE schemaname = %s ORDER BY tablename",
            (CHECKPOINT_SCHEMA,),
        ).fetchall()
        found = {row["tablename"] for row in rows}

        # The application role needs DML on tables it did not create, and on
        # tables the next `setup()` will create. Default privileges cover the
        # future ones; the explicit grant covers the ones that exist now.
        conn.execute(f"GRANT USAGE ON SCHEMA {CHECKPOINT_SCHEMA} TO os_app")
        conn.execute(
            f"GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES "
            f"IN SCHEMA {CHECKPOINT_SCHEMA} TO os_app"
        )
        conn.execute(
            f"ALTER DEFAULT PRIVILEGES IN SCHEMA {CHECKPOINT_SCHEMA} "
            f"GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO os_app"
        )

    print(f"schema {CHECKPOINT_SCHEMA}: {len(found)} table(s)")
    for table in sorted(found):
        print(f"  {table}")

    missing = set(CHECKPOINT_TABLES) - found
    if missing:
        raise SystemExit(f"expected tables are missing: {sorted(missing)}")
    print("\nready. os_app has DML on this schema and no DDL, as intended.")


if __name__ == "__main__":
    asyncio.run(asyncio.to_thread(main))
