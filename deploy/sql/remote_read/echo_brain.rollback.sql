-- #3787 rollback for echo_brain.sql: removes the views and every grant they added.
BEGIN;
DROP SCHEMA IF EXISTS remote_read CASCADE;
REVOKE SELECT ON public.tower_tasks, public.tower_projects, public.tower_task_runs,
                  public.operator_questions, public.notifications
  FROM tower_remote_read_owner;
REVOKE USAGE ON SCHEMA public FROM tower_remote_read_owner;
COMMIT;
