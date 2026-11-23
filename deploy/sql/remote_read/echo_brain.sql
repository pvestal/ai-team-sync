-- #3787 remote read-only connector: views in echo_brain.
-- Prepared here as the connector's read contract; landed in the Echo repo as
-- its next-numbered migration by the Echo owner at release, then applied by
-- the release step. Requires 00_roles.sql. Idempotent.
--
-- Each view is a COLUMN ALLOWLIST: a column not named here cannot reach a
-- remote client. Excluded on purpose (and pinned by tests/test_remote_read_sql_3787.py):
--   tower_tasks.description            (served by Echo's own envelope route instead)
--   tower_task_runs: session_id, repo_root, worktree_path, target_file,
--                    research/implementation/tests/review/landing/verification jsonb
--   operator_questions: nonce, delivery_token_hash, chat_id, telegram_message_id,
--                    answer_user_id, ats_session_id, evidence, delivery, decision_id
--   notifications: metadata (holds per-channel results), every non-outbox source
--   echo_telegram_messages, telegram_inbox: not exposed at all (personal content)

BEGIN;

-- Fail closed (#3787 review r1): a remote_read schema that already holds any
-- object other than the reviewed views refuses this migration, so nothing
-- unexpected can inherit the connector's grants.
DO $$
DECLARE stray text;
BEGIN
  SELECT string_agg(c.relname, ', ') INTO stray
    FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
   WHERE n.nspname = 'remote_read'
     AND c.relname NOT IN ('tower_tasks', 'tower_task_runs', 'operator_questions', 'notification_outbox');
  IF stray IS NULL THEN
    SELECT string_agg(p.proname, ', ') INTO stray
      FROM pg_proc p JOIN pg_namespace n ON n.oid = p.pronamespace
     WHERE n.nspname = 'remote_read';
  END IF;
  IF stray IS NOT NULL THEN
    RAISE EXCEPTION 'remote_read holds unreviewed objects: %', stray;
  END IF;
END $$;

CREATE SCHEMA IF NOT EXISTS remote_read AUTHORIZATION tower_remote_read_owner;
REVOKE ALL ON SCHEMA remote_read FROM PUBLIC;
GRANT USAGE ON SCHEMA remote_read TO tower_remote_ro;

-- The view owner may read exactly these base tables.
GRANT USAGE ON SCHEMA public TO tower_remote_read_owner;
GRANT SELECT ON public.tower_tasks, public.tower_projects, public.tower_task_runs,
                public.operator_questions, public.notifications
  TO tower_remote_read_owner;

-- The connector role gets no schema it does not need, and never CREATE.
REVOKE CREATE ON SCHEMA public FROM tower_remote_ro;

SET LOCAL ROLE tower_remote_read_owner;

CREATE OR REPLACE VIEW remote_read.tower_tasks WITH (security_barrier) AS
SELECT t.id, t.project_id, p.name AS project_name, t.task_key, t.title,
       t.status, t.gate, t.priority, t.blocked_by, t.parent_id,
       left(t.recommendation, 2000) AS recommendation,
       left(t.notes, 4000) AS notes,
       t.created_at, t.updated_at
FROM public.tower_tasks t
LEFT JOIN public.tower_projects p ON p.id = t.project_id;

CREATE OR REPLACE VIEW remote_read.tower_task_runs WITH (security_barrier) AS
SELECT r.id, r.task_id, r.state, r.executor, r.level, r.branch, r.base_sha,
       r.final_sha, r.disposition, left(r.error, 1000) AS error,
       r.claimed_at, r.heartbeat_at, r.lease_expires_at, r.card_sent_at,
       r.created_at, r.updated_at, r.finished_at
FROM public.tower_task_runs r;

CREATE OR REPLACE VIEW remote_read.operator_questions WITH (security_barrier) AS
SELECT q.id, q.tower_task_id, q.question_key, q.question, q.options,
       left(q.context::text, 2000) AS context, q.info_only, q.test, q.state,
       q.message_kind, q.send_attempts, left(q.last_send_error, 300) AS last_send_error,
       q.sent_at, q.delivered_at, q.answered_at, q.answer_index, q.created_at
FROM public.operator_questions q;

-- The Telegram/notification outbox written by Echo's send_notification path
-- (mcp_service.py). Channel NAMES only; per-channel results stay out.
CREATE OR REPLACE VIEW remote_read.notification_outbox WITH (security_barrier) AS
SELECT n.id, n.title, left(n.body, 1000) AS body, n.priority, n.category,
       n.status, ARRAY(SELECT jsonb_array_elements_text(
                         CASE WHEN jsonb_typeof(n.metadata -> 'channels') = 'array'
                              THEN n.metadata -> 'channels' ELSE '[]'::jsonb END)) AS channels,
       n.created_at, n.delivered_at, n.read_at, n.scheduled_for
FROM public.notifications n
WHERE n.source = 'mcp_send_notification';

RESET ROLE;

-- Named grants only (#3787 review r1): never ALL TABLES IN SCHEMA.
GRANT SELECT ON remote_read.tower_tasks TO tower_remote_ro;
GRANT SELECT ON remote_read.tower_task_runs TO tower_remote_ro;
GRANT SELECT ON remote_read.operator_questions TO tower_remote_ro;
GRANT SELECT ON remote_read.notification_outbox TO tower_remote_ro;

-- Large-object creation is the one persistent write PostgreSQL grants to
-- PUBLIC by default (#3787 review r1: a session that turns its own read-only
-- default off could lo_create/lo_from_bytea). Measured 2026-11-23: 0 large
-- objects in this database and no Echo/Anime Studio code uses them;
-- superusers are unaffected. Rollback re-grants them to PUBLIC.
REVOKE EXECUTE ON FUNCTION
  pg_catalog.lo_creat(integer),
  pg_catalog.lo_create(oid),
  pg_catalog.lo_from_bytea(oid, bytea),
  pg_catalog.lo_put(oid, bigint, bytea),
  pg_catalog.lowrite(integer, bytea),
  pg_catalog.lo_truncate(integer, integer),
  pg_catalog.lo_truncate64(integer, bigint),
  pg_catalog.lo_unlink(oid),
  pg_catalog.lo_open(oid, integer)
  FROM PUBLIC;

COMMIT;
