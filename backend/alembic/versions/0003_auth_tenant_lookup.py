"""Authentication tenant lookup.

Revision ID: 0003_auth_lookup
Revises: 0002_tickets
Create Date: 2026-08-31

Solves a real conflict between login and row-level security.

``users`` is tenant-scoped with FORCE RLS, so a query with no ``app.tenant_id``
set returns zero rows -- which is exactly right, and exactly the problem at
login: the tenant is not known until the user is found.

The narrowest safe answer is a SECURITY DEFINER function that answers one
question and nothing else: *which tenant does this email belong to?* It returns
a single uuid -- no password hash, no name, no other column -- and the caller
then sets the tenant scope and loads the user through ordinary RLS.

Alternatives rejected:

* Granting BYPASSRLS to the application role -- disables isolation everywhere,
  for one query.
* A policy allowing reads when the scope is unset -- inverts the fail-closed
  default that makes the whole design safe.
* A separate unscoped email->tenant table -- a second copy of the truth, which
  will drift.
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0003_auth_lookup"
down_revision: str | None = "0002_tickets"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute(
        """
        CREATE OR REPLACE FUNCTION auth_tenant_for_email(p_email text)
        RETURNS uuid
        LANGUAGE sql
        STABLE
        SECURITY DEFINER
        -- Pinned search_path: a SECURITY DEFINER function without one can be
        -- hijacked by a caller-controlled schema shadowing 'users'.
        SET search_path = public, pg_temp
        AS $$
            SELECT tenant_id
            FROM users
            WHERE lower(email) = lower(p_email)
              AND deleted_at IS NULL
            LIMIT 1
        $$;
        """
    )

    # Callable by the application role, and by nobody else.
    op.execute("REVOKE ALL ON FUNCTION auth_tenant_for_email(text) FROM PUBLIC")
    op.execute(
        """
        DO $$
        BEGIN
            IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'os_app') THEN
                GRANT EXECUTE ON FUNCTION auth_tenant_for_email(text) TO os_app;
            END IF;
        END
        $$
        """
    )


def downgrade() -> None:
    op.execute("DROP FUNCTION IF EXISTS auth_tenant_for_email(text)")
