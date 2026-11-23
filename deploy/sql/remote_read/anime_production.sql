-- #3787 remote read-only connector: views in anime_production.
-- Prepared here as the connector's read contract; landed in the Anime Studio
-- repo as its next-numbered migration by the Anime Studio owner at release,
-- then applied by the release step. Requires 00_roles.sql. Idempotent.
--
-- Why views and not Anime Studio's HTTP API: under ANIME_STUDIO_AUTH_MODE=
-- single_user every request to :8401 is the configured operator, and several
-- of its GET routes write (review-queue / pending-keyframes / fidelity-report
-- start VLM QC, pending-videos UPDATEs shots, /api/system/gpu/arbiter sweeps
-- gpu_claims, video-evidence builds files). These views are plain SELECTs.
--
-- Each view is a COLUMN ALLOWLIST. Excluded on purpose (pinned by
-- tests/test_remote_read_sql_3787.py): every *_path column and prompt text;
-- production_assignment_items revision/qc/result; submissions payload,
-- graph_facts, assets, binding_path, backend_reported; stop_choices Telegram
-- ids (telegram_chat_id, button_message_id, callback_query_id, from_user_id);
-- dispatch_holds provenance jsonb; gpu_claims holder_host/pid/boot_id/
-- start_ticks/cmd; render_admission_executions credential_hash, binding_id,
-- bound_principal_id, state_fingerprint, rules, evidence; refusals proof.

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
     AND c.relname NOT IN ('production_assignments', 'production_assignment_items', 'production_assignment_submissions', 'production_assignment_stop_choices', 'dispatch_holds', 'judge_queue', 'accepted_media', 'gpu_claims', 'render_admission_executions', 'render_admission_refusals', 'video_submissions');
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

GRANT USAGE ON SCHEMA public TO tower_remote_read_owner;
GRANT SELECT ON public.production_assignments, public.production_assignment_items,
                public.production_assignment_submissions,
                public.production_assignment_stop_choices, public.dispatch_holds,
                public.shots, public.scenes, public.projects, public.gpu_claims,
                public.render_admission_executions,
                public.render_admission_execution_refusals, public.video_submissions
  TO tower_remote_read_owner;

REVOKE CREATE ON SCHEMA public FROM tower_remote_ro;

SET LOCAL ROLE tower_remote_read_owner;

CREATE OR REPLACE VIEW remote_read.production_assignments WITH (security_barrier) AS
SELECT a.id, a.request_key, a.scope, a.render_budget, a.attempt_budget,
       a.review_limit, a.expires_at, a.stop_conditions,
       left(a.directions, 2000) AS directions, a.status, a.status_reason,
       a.created_by, a.principal, a.created_at, a.updated_at
FROM public.production_assignments a;

CREATE OR REPLACE VIEW remote_read.production_assignment_items WITH (security_barrier) AS
SELECT i.id, i.assignment_id, i.shot_id, i.keyframe_sha, i.action, i.state,
       i.attempts, i.renders, i.job_id, left(i.reason, 1000) AS reason,
       i.cycle_id, i.claimed_at, i.committed_at, i.created_at, i.updated_at
FROM public.production_assignment_items i;

CREATE OR REPLACE VIEW remote_read.production_assignment_submissions WITH (security_barrier) AS
SELECT s.id, s.item_id, s.assignment_id, s.shot_id, s.seq, s.execution_id,
       s.root_execution_id, s.admission_class, s.selected_method, s.backend_id,
       s.workflow_sha256, s.state, s.prompt_id, s.claimed_at, s.settled_at
FROM public.production_assignment_submissions s;

CREATE OR REPLACE VIEW remote_read.production_assignment_stop_choices WITH (security_barrier) AS
SELECT c.id, c.stopped_assignment_id, c.decision, c.outcome, c.new_assignment_id,
       c.stop_status, left(c.stop_reason, 1000) AS stop_reason, c.created_at
FROM public.production_assignment_stop_choices c;

CREATE OR REPLACE VIEW remote_read.dispatch_holds WITH (security_barrier) AS
SELECT h.id, h.scope_kind, h.scope_id, left(h.reason, 1000) AS reason, h.owner,
       h.placed_by, h.placed_at, h.expires_at, h.released_at, h.released_by,
       left(h.release_reason, 1000) AS release_reason,
       (h.released_at IS NULL AND (h.expires_at IS NULL OR h.expires_at > now())) AS active
FROM public.dispatch_holds h;

-- Judge queue membership, DB side only. Mirrors Anime Studio 776e213d9
-- packages/scene_generation/scene_review.py:
--   video lane    = PENDING_VIDEO_SHOT_BASE_CONDITIONS (:573) incl.
--                   VIDEO_ARTIFACT_UNJUDGED_SQL (:534)
--   keyframe lane = get_review_queue conditions (:4579) incl. HAS_KEYFRAME_SQL
--                   (:492) and _VIDEO_LANE_SQL_SH (:522)
-- Known differences from the live Judge page (documented, not hidden): the
-- keyframe lane omits the legacy-linked candidate arm (a Python-computed id
-- list), and neither lane checks that the file exists on disk or resolves a
-- missing video identity (the route does that lazily, and writes).
CREATE OR REPLACE VIEW remote_read.judge_queue WITH (security_barrier) AS
SELECT 'video'::text AS lane, sh.id AS shot_id, sh.scene_id, sc.title AS scene_title,
       sc.project_id, p.name AS project_name, sh.shot_number, sh.shot_type,
       sh.characters_present, sh.composition_method, sh.video_engine,
       sh.output_video_visual_sha, sh.review_status AS machine_review_status,
       sh.operator_verdict, sh.operator_verdict_by, sh.operator_verdict_at,
       sh.video_rendered_at, sh.created_at
FROM public.shots sh
JOIN public.scenes sc ON sc.id = sh.scene_id
JOIN public.projects p ON p.id = sc.project_id
WHERE (sh.operator_verdict IS NULL
       OR (sh.operator_verdict = 'approved' AND sh.operator_verdict_by LIKE 'review-keyframe%')
       OR (sh.video_verdict_visual_sha IS NOT NULL AND sh.output_video_visual_sha IS NOT NULL
           AND sh.video_verdict_visual_sha <> sh.output_video_visual_sha)
       OR (sh.video_verdict_visual_sha IS NOT NULL AND sh.output_video_visual_sha IS NULL)
       OR (sh.video_verdict_visual_sha IS NULL
           AND (sh.operator_verdict_by LIKE 'review-video%'
                OR sh.operator_verdict_by LIKE 'batch-review-video%')
           AND sh.video_rendered_at IS NOT NULL
           AND sh.operator_verdict_at IS NOT NULL
           AND sh.operator_verdict_at < sh.video_rendered_at))
  AND sh.output_video_path IS NOT NULL
  AND COALESCE(sh.status, '') NOT IN ('archived', 'abandoned')
  AND COALESCE(sh.status, '') <> 'generating'
  AND COALESCE(sc.status, '') <> 'archived'
UNION ALL
SELECT 'keyframe'::text, sh.id, sh.scene_id, sc.title, sc.project_id, p.name,
       sh.shot_number, sh.shot_type, sh.characters_present, sh.composition_method,
       sh.video_engine, sh.output_video_visual_sha, sh.review_status,
       sh.operator_verdict, sh.operator_verdict_by, sh.operator_verdict_at,
       sh.video_rendered_at, sh.created_at
FROM public.shots sh
JOIN public.scenes sc ON sc.id = sh.scene_id
JOIN public.projects p ON p.id = sc.project_id
WHERE COALESCE(NULLIF(BTRIM(sh.first_frame_path, E' \t\r\n'), ''),
               NULLIF(BTRIM(sh.source_image_path, E' \t\r\n'), '')) IS NOT NULL
  AND (sh.operator_verdict IS NULL
       OR COALESCE(sh.operator_verdict_by, '') LIKE 'review-video%'
       OR COALESCE(sh.operator_verdict_by, '') LIKE 'batch-review-video%')
  AND sh.output_video_path IS NULL
  AND sh.video_rendered_at IS NULL
  AND COALESCE(sh.status, '') NOT IN ('archived', 'abandoned')
  AND COALESCE(sc.status, '') <> 'archived';

-- Accepted media RECORDS (no bytes): Anime Studio's approved-clips partition
-- (scene_review.py:2730 list_approved_clips) -- review_status='approved' with a
-- video, not archived/abandoned. artifact_visual_sha uses the route's own
-- precedence. operator_verdict is carried so a reader can tell an operator
-- approval from a machine-written review_status.
CREATE OR REPLACE VIEW remote_read.accepted_media WITH (security_barrier) AS
SELECT sh.id AS shot_id, sh.scene_id, sc.title AS scene_title, sc.project_id,
       p.name AS project_name, sh.shot_number, sh.duration_seconds,
       sh.characters_present, sh.video_engine, sh.reviewed_at, sh.video_rendered_at,
       COALESCE(sh.video_verdict_visual_sha, sh.approved_video_visual_sha,
                sh.output_video_visual_sha) AS artifact_visual_sha,
       sh.operator_verdict, sh.operator_verdict_by, sh.operator_verdict_at
FROM public.shots sh
JOIN public.scenes sc ON sc.id = sh.scene_id
JOIN public.projects p ON p.id = sc.project_id
WHERE sh.review_status = 'approved'
  AND sh.output_video_path IS NOT NULL
  AND sh.status NOT IN ('archived', 'abandoned');

CREATE OR REPLACE VIEW remote_read.gpu_claims WITH (security_barrier) AS
SELECT g.claim_id, g.claim_type, g.card, g.caller, g.holder_kind,
       g.estimated_duration_s, g.model_needed, g.vram_need_mb, g.claimed_at,
       g.renewed_at, g.released_at, g.release_reason, g.released_by,
       (g.released_at IS NULL) AS active
FROM public.gpu_claims g;

CREATE OR REPLACE VIEW remote_read.render_admission_executions WITH (security_barrier) AS
SELECT e.execution_id, e.admission_class, e.outcome, e.reason_code, e.shot_id,
       e.beat_id, e.selected_method, e.parent_execution_id, e.workflow_sha256,
       e.backend_id, e.bound_project_id, e.profile_version, e.credential_expires_at,
       e.consumed_at, e.dispatch_state, e.created_at
FROM public.render_admission_executions e;

CREATE OR REPLACE VIEW remote_read.render_admission_refusals WITH (security_barrier) AS
SELECT r.execution_id, r.shot_id, r.backend_id, r.comfyui_prompt_id, r.reason_code,
       left(r.refusal_text, 500) AS refusal_text, r.recorded_by, r.recorded_at
FROM public.render_admission_execution_refusals r;

CREATE OR REPLACE VIEW remote_read.video_submissions WITH (security_barrier) AS
SELECT v.submission_id, v.shot_id, v.keyframe_sha, v.requested_by, v.outcome,
       v.engine, v.error_code, left(v.error, 500) AS error, v.comfyui_prompt_id,
       v.backend_id, v.backend_kind, v.created_at, v.finished_at, v.resolved_at,
       v.last_reconciled_at, left(v.reconcile_note, 300) AS reconcile_note
FROM public.video_submissions v;

RESET ROLE;

-- Named grants only (#3787 review r1): never ALL TABLES IN SCHEMA.
GRANT SELECT ON remote_read.production_assignments TO tower_remote_ro;
GRANT SELECT ON remote_read.production_assignment_items TO tower_remote_ro;
GRANT SELECT ON remote_read.production_assignment_submissions TO tower_remote_ro;
GRANT SELECT ON remote_read.production_assignment_stop_choices TO tower_remote_ro;
GRANT SELECT ON remote_read.dispatch_holds TO tower_remote_ro;
GRANT SELECT ON remote_read.judge_queue TO tower_remote_ro;
GRANT SELECT ON remote_read.accepted_media TO tower_remote_ro;
GRANT SELECT ON remote_read.gpu_claims TO tower_remote_ro;
GRANT SELECT ON remote_read.render_admission_executions TO tower_remote_ro;
GRANT SELECT ON remote_read.render_admission_refusals TO tower_remote_ro;
GRANT SELECT ON remote_read.video_submissions TO tower_remote_ro;

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
