-- #3787 rollback for echo_brain.sql: removes the views and every grant they added.
BEGIN;
DROP SCHEMA IF EXISTS remote_read CASCADE;
REVOKE SELECT ON public.tower_tasks, public.tower_projects, public.tower_task_runs,
                  public.operator_questions, public.notifications
  FROM tower_remote_read_owner;
REVOKE USAGE ON SCHEMA public FROM tower_remote_read_owner;
GRANT EXECUTE ON FUNCTION
  pg_catalog.lo_creat(integer),
  pg_catalog.lo_create(oid),
  pg_catalog.lo_from_bytea(oid, bytea),
  pg_catalog.lo_put(oid, bigint, bytea),
  pg_catalog.lowrite(integer, bytea),
  pg_catalog.lo_truncate(integer, integer),
  pg_catalog.lo_truncate64(integer, bigint),
  pg_catalog.lo_unlink(oid),
  pg_catalog.lo_open(oid, integer)
  TO PUBLIC;
COMMIT;
