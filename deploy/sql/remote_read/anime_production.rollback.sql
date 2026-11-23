-- #3787 rollback for anime_production.sql: removes the views and every grant they added.
BEGIN;
DROP SCHEMA IF EXISTS remote_read CASCADE;
REVOKE SELECT ON public.production_assignments, public.production_assignment_items,
                  public.production_assignment_submissions,
                  public.production_assignment_stop_choices, public.dispatch_holds,
                  public.shots, public.scenes, public.projects, public.gpu_claims,
                  public.render_admission_executions,
                  public.render_admission_execution_refusals, public.video_submissions
  FROM tower_remote_read_owner;
REVOKE USAGE ON SCHEMA public FROM tower_remote_read_owner;
COMMIT;
