-- Database bootstrap for OS Tracker.
--
-- Run ONCE as a Postgres superuser before the first migration.
--
--   psql -U postgres -f scripts/bootstrap_database.sql
--
-- Creates two roles, and this separation is not optional:
--
--   os_migrate  owns the schema. Alembic connects as this role.
--   os_app      owns nothing. The application connects as this role.
--
-- Row-level security is bypassed by superusers AND by the table owner. If the
-- application connected as the role that ran the migrations, every RLS policy
-- would be decorative and nobody would notice until an audit.
-- See docs/04-database.md.

-- ---------------------------------------------------------------- roles

DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'os_migrate') THEN
        CREATE ROLE os_migrate LOGIN PASSWORD 'os_migrate_dev_2026';
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'os_app') THEN
        CREATE ROLE os_app LOGIN PASSWORD 'os_app_dev_2026';
    END IF;
END
$$;

-- os_migrate owns the schema and needs BYPASSRLS for one specific reason: the
-- SECURITY DEFINER login lookup (migration 0003) runs as this role, and the
-- tables it reads use FORCE ROW LEVEL SECURITY -- which applies to the table
-- owner as well. Without this, login cannot resolve a user's tenant at all.
--
-- This is safe because os_migrate is already fully privileged (it can drop any
-- table) and the application never connects as it. The application role,
-- os_app, must NEVER be granted this.
ALTER ROLE os_migrate BYPASSRLS;
ALTER ROLE os_app NOBYPASSRLS;

-- ------------------------------------------------------------- database

SELECT 'CREATE DATABASE os_tracker OWNER os_migrate'
WHERE NOT EXISTS (SELECT 1 FROM pg_database WHERE datname = 'os_tracker')\gexec

\connect os_tracker

-- --------------------------------------------------------------- schema

ALTER SCHEMA public OWNER TO os_migrate;

REVOKE ALL ON SCHEMA public FROM PUBLIC;
GRANT USAGE ON SCHEMA public TO os_app;

-- os_app may read and write rows but never change structure.
GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA public TO os_app;
GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA public TO os_app;

-- Same grants automatically for anything os_migrate creates later, so a new
-- migration does not require remembering to re-grant.
ALTER DEFAULT PRIVILEGES FOR ROLE os_migrate IN SCHEMA public
    GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO os_app;
ALTER DEFAULT PRIVILEGES FOR ROLE os_migrate IN SCHEMA public
    GRANT USAGE, SELECT ON SEQUENCES TO os_app;

-- ----------------------------------------------------------- extensions

CREATE EXTENSION IF NOT EXISTS "citext";      -- case-insensitive email columns
CREATE EXTENSION IF NOT EXISTS "pg_trgm";     -- fuzzy matching for search later

-- Note: os_app is deliberately NOT granted BYPASSRLS, and must never be.
