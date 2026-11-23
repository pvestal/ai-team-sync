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

GRANT SELECT ON ALL TABLES IN SCHEMA remote_read TO tower_remote_ro;

COMMIT;
