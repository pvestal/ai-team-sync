-- #3787 remote read-only connector: cluster roles. Run ONCE, as a superuser, at
-- release (never by the connector). Roles are cluster-wide in PostgreSQL; the
-- per-database files grant them only the remote_read views.
--
--   tower_remote_read_owner  NOLOGIN. Owns the remote_read schema and its views.
--                            A view reads its base tables with its OWNER's
--                            rights, so this role holds SELECT on exactly the
--                            base tables the views name, and nothing else.
--   tower_remote_ro          LOGIN, used by ats-mcp-remote. It can read the
--                            remote_read views and nothing else: no base table,
--                            no other schema, no write, no DDL.
--
-- The password is NOT set here. Release sets it from Vault:
--   ALTER ROLE tower_remote_ro PASSWORD '<from vault>';   (via psql \password)

DO $$
BEGIN
  IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'tower_remote_read_owner') THEN
    CREATE ROLE tower_remote_read_owner NOLOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE
      NOINHERIT NOREPLICATION NOBYPASSRLS;
  END IF;
  IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'tower_remote_ro') THEN
    CREATE ROLE tower_remote_ro LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE
      NOINHERIT NOREPLICATION NOBYPASSRLS CONNECTION LIMIT 4;
  END IF;
END $$;

-- Defence in depth: every session of the connector role starts read-only and
-- bounded. (The connector also opens each query in a READ ONLY transaction.)
ALTER ROLE tower_remote_ro SET default_transaction_read_only = on;
ALTER ROLE tower_remote_ro SET statement_timeout = '3s';
ALTER ROLE tower_remote_ro SET idle_in_transaction_session_timeout = '5s';
ALTER ROLE tower_remote_ro SET lock_timeout = '1s';
ALTER ROLE tower_remote_ro SET search_path = pg_catalog;
