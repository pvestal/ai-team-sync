"""#3787: the remote_read role and views against a PRIVATE PostgreSQL sandbox.

Skipped unless deploy/sql/remote_read/sandbox.sh has built a sandbox and its
env line is exported:

    deploy/sql/remote_read/sandbox.sh <empty-dir>
    export REMOTE_READ_SANDBOX_HOST=<socket dir> REMOTE_READ_SANDBOX_PORT=55439
    pytest tests/test_remote_read_sql_3787.py

The sandbox holds the real table SHAPES (schema-only dump) and no production
rows. Proves: the views compile against today's schemas; the role cannot read
base tables, write, run DDL, read files or run programs; every connector query
runs; and running every tool changes no row and no table counter.
"""

from __future__ import annotations

import asyncio
import json
import os
import stat

import pytest

asyncpg = pytest.importorskip("asyncpg")

from ai_team_sync.mcp import remote_tower as tower  # noqa: E402

HOST = os.environ.get("REMOTE_READ_SANDBOX_HOST")
PORT = int(os.environ.get("REMOTE_READ_SANDBOX_PORT", "55439"))
pytestmark = pytest.mark.skipif(not HOST, reason="no remote_read sandbox (see module doc)")
RO = {"user": "tower_remote_ro", "password": "sandbox-only"}

ECHO_VIEWS = {"tower_tasks", "tower_task_runs", "operator_questions", "notification_outbox"}
ANIME_VIEWS = {"production_assignments", "production_assignment_items",
               "production_assignment_submissions", "production_assignment_stop_choices",
               "dispatch_holds", "judge_queue", "accepted_media", "gpu_claims",
               "render_admission_executions", "render_admission_refusals", "video_submissions"}
# Columns that must never appear in any remote_read view.
FORBIDDEN_COLUMNS = {
    "description", "session_id", "repo_root", "worktree_path", "target_file", "research",
    "implementation", "review", "landing", "verification", "nonce", "delivery_token_hash",
    "chat_id", "telegram_message_id", "answer_user_id", "ats_session_id", "evidence",
    "delivery", "metadata", "revision", "qc", "result", "payload", "graph_facts", "assets",
    "binding_path", "backend_reported", "telegram_chat_id", "button_message_id",
    "callback_query_id", "from_user_id", "placement_provenance", "release_provenance",
    "holder_host", "holder_pid", "holder_boot_id", "holder_start_ticks", "holder_cmd",
    "credential_hash", "binding_id", "bound_principal_id", "state_fingerprint", "rules",
    "proof", "generation_prompt", "motion_prompt", "source_image_path", "first_frame_path",
    "output_video_path", "sfx_audio_path", "execution_ref", "user_message", "bot_response",
}


async def _conn(db: str, **who):
    return await asyncpg.connect(host=HOST, port=PORT, database=db, **(who or RO))


async def _super(db: str):
    return await asyncpg.connect(host=HOST, port=PORT, database=db, user="sandbox_super")


@pytest.fixture
def sandbox_connector(monkeypatch, tmp_path):
    cfg = tmp_path / "db.json"
    cfg.write_text(json.dumps({**RO, "host": HOST, "port": PORT}))
    os.chmod(cfg, stat.S_IRUSR | stat.S_IWUSR)
    monkeypatch.setattr(tower, "DB_FILE", cfg)
    monkeypatch.setattr(tower, "connect", tower._connect)
    monkeypatch.setattr(tower, "_verified", set())


# ---- contract --------------------------------------------------------------------------

@pytest.mark.parametrize("db,views", [("echo_brain", ECHO_VIEWS), ("anime_production", ANIME_VIEWS)])
async def test_views_are_exactly_the_reviewed_set_and_omit_forbidden_columns(db, views):
    c = await _conn(db)
    try:
        rows = await c.fetch("SELECT table_name, column_name FROM information_schema.columns "
                             "WHERE table_schema = 'remote_read'")
    finally:
        await c.close()
    assert {r["table_name"] for r in rows} == views
    leaked = {(r["table_name"], r["column_name"]) for r in rows
              if r["column_name"] in FORBIDDEN_COLUMNS}
    assert not leaked, leaked


async def test_role_self_check_passes_in_both_databases(sandbox_connector):
    for key in tower.DATABASES:
        c = await tower.connect(key)
        try:
            await tower._verify_role(c, key)
        finally:
            await c.close()


# ---- denials ------------------------------------------------------------------------------

DENIED = {
    "echo_brain": [
        "SELECT * FROM public.tower_tasks LIMIT 1",
        "SELECT * FROM public.echo_telegram_messages LIMIT 1",
        "SELECT * FROM public.telegram_inbox LIMIT 1",
        "SELECT nonce FROM public.operator_questions LIMIT 1",
        "INSERT INTO remote_read.tower_tasks (id) VALUES (1)",
        "UPDATE remote_read.operator_questions SET state = 'x'",
        "DELETE FROM remote_read.notification_outbox",
    ],
    "anime_production": [
        "SELECT * FROM public.shots LIMIT 1",
        "UPDATE public.shots SET review_status = 'approved'",
        "INSERT INTO remote_read.gpu_claims (claim_id) VALUES (gen_random_uuid())",
        "DELETE FROM remote_read.dispatch_holds",
        "TRUNCATE remote_read.judge_queue",
    ],
}
DENIED_EVERYWHERE = [
    "CREATE TABLE remote_read.x (i int)",
    "CREATE TABLE public.x (i int)",
    "CREATE TEMP TABLE x (i int)",
    "CREATE SCHEMA x",
    "CREATE FUNCTION remote_read.f() RETURNS int LANGUAGE sql AS 'select 1'",
    "SELECT pg_read_file('/etc/passwd')",
    "SELECT pg_ls_dir('/')",
    "COPY (SELECT 1) TO PROGRAM 'true'",
    "COPY (SELECT 1) TO '/tmp/rr-should-not-exist'",
    "SELECT lo_import('/etc/passwd')",
    "ALTER ROLE tower_remote_ro SET default_transaction_read_only = off",
    "GRANT SELECT ON remote_read.tower_tasks TO PUBLIC",
    "SET ROLE tower_remote_read_owner",
]


@pytest.mark.parametrize("db,sql", [(db, q) for db, qs in DENIED.items() for q in qs]
                         + [(db, q) for db in DENIED for q in DENIED_EVERYWHERE])
async def test_denied(db, sql):
    c = await _conn(db)
    try:
        with pytest.raises(asyncpg.PostgresError):
            await c.execute(sql)
    finally:
        await c.close()


async def test_session_flipped_to_read_write_still_cannot_touch_any_table():
    """A session that turns its own read-only default off can still write nothing
    persistent. The one thing it CAN do is create a session-local TEMP table:
    PostgreSQL grants TEMPORARY on every database to PUBLIC by default, and a
    PUBLIC grant cannot be withdrawn from one role. Documented residual: the
    connector never sends anything but its fixed SELECTs inside READ ONLY
    transactions, and revoking TEMPORARY from PUBLIC is a release-time option."""
    c = await _conn("anime_production")
    try:
        await c.execute("SET default_transaction_read_only = off")
        for sql in ("CREATE TABLE public.x (i int)", "CREATE TABLE remote_read.x (i int)",
                    "UPDATE public.shots SET status = status",
                    "INSERT INTO remote_read.gpu_claims (claim_id) VALUES (gen_random_uuid())"):
            with pytest.raises(asyncpg.PostgresError):
                await c.execute(sql)
        await c.execute("CREATE TEMP TABLE x (i int)")  # PUBLIC TEMPORARY: see docstring
    finally:
        await c.close()


async def test_cannot_signal_another_backend():
    su = await _super("echo_brain")
    c = await _conn("echo_brain")
    try:
        with pytest.raises(asyncpg.PostgresError):
            await c.execute(f"SELECT pg_terminate_backend({su.get_server_pid()})")
        assert not su.is_closed()
    finally:
        await c.close()
        await su.close()


async def test_statement_timeout_bounds_a_query():
    c = await _conn("echo_brain")
    try:
        with pytest.raises(asyncpg.QueryCanceledError):
            await c.execute("SELECT pg_sleep(5)")
    finally:
        await c.close()


async def test_session_defaults_are_read_only_and_bounded():
    c = await _conn("echo_brain")
    try:
        assert await c.fetchval("SHOW default_transaction_read_only") == "on"
        assert await c.fetchval("SHOW statement_timeout") == "3s"
        assert await c.fetchval("SHOW search_path") == "pg_catalog"
    finally:
        await c.close()


# ---- every connector query runs, and nothing changes ----------------------------------------

_FILL = {"uuid": "gen_random_uuid()", "integer": "1", "bigint": "1", "smallint": "1",
         "numeric": "1", "double precision": "1", "real": "1", "boolean": "false",
         "text": "'x'", "character varying": "'x'", "jsonb": "'{}'", "json": "'{}'",
         "timestamp with time zone": "now()", "timestamp without time zone": "now()",
         "date": "current_date", "ARRAY": "'{}'"}


async def _seed(db: str) -> list[str]:
    """One generic row per base table, FK/triggers off (superuser, sandbox only)."""
    c = await _super(db)
    seeded = []
    try:
        await c.execute("SET session_replication_role = replica")
        tables = [r["relname"] for r in await c.fetch(
            "SELECT relname FROM pg_stat_user_tables WHERE schemaname = 'public'")]
        for t in tables:
            cols = await c.fetch(
                "SELECT column_name, data_type FROM information_schema.columns "
                "WHERE table_schema = 'public' AND table_name = $1 AND is_nullable = 'NO' "
                "AND column_default IS NULL", t)
            names = [r["column_name"] for r in cols if r["data_type"] in _FILL]
            if len(names) != len(cols):
                continue
            vals = [_FILL[r["data_type"]] for r in cols]
            sql = (f'INSERT INTO public."{t}" ({", ".join(chr(34) + n + chr(34) for n in names)}) '
                   f'VALUES ({", ".join(vals)})' if names else f'INSERT INTO public."{t}" DEFAULT VALUES')
            try:
                await c.execute(sql)
                seeded.append(t)
            except asyncpg.PostgresError:
                pass  # a CHECK constraint we did not satisfy: that table stays empty
    finally:
        await c.close()
    return seeded


async def _fingerprint(db: str) -> dict:
    c = await _super(db)
    try:
        await asyncio.sleep(1.2)  # backend exits flush table stats; let them land
        await c.execute("SELECT pg_stat_clear_snapshot()")
        stats = {r["relname"]: (r["n_tup_ins"], r["n_tup_upd"], r["n_tup_del"])
                 for r in await c.fetch("SELECT relname, n_tup_ins, n_tup_upd, n_tup_del "
                                        "FROM pg_stat_user_tables WHERE schemaname = 'public'")}
        content = {}
        for t in stats:
            content[t] = await c.fetchval(
                f'SELECT md5(coalesce(string_agg(x::text, \'|\' ORDER BY x::text), \'\')) '
                f'FROM public."{t}" x')
        return {"stats": stats, "content": content}
    finally:
        await c.close()


TOOL_CALLS = {
    "tower_task_runs": {"task_id": 1}, "open_tasks": {}, "operator_questions": {},
    "notification_outbox": {}, "production_assignments": {}, "dispatch_holds": {"active_only": False},
    "judge_queue": {}, "accepted_media": {}, "gpu_claims": {"active_only": False},
    "governed_executions": {}, "video_submissions": {},
}


async def test_every_tool_runs_and_mutates_nothing(sandbox_connector):
    seeded = {db: await _seed(db) for db in ("echo_brain", "anime_production")}
    assert seeded["anime_production"] and seeded["echo_brain"], seeded
    before = {db: await _fingerprint(db) for db in seeded}
    for name, args in TOOL_CALLS.items():
        out = await tower.TOWER_HANDLERS[name](None, args, "http://unused")
        assert out and out[0].text, name
    out = await tower.TOWER_HANDLERS["production_assignments"](
        None, {"assignment_id": "00000000-0000-0000-0000-000000000000"}, "http://unused")
    assert "No assignment" in out[0].text
    after = {db: await _fingerprint(db) for db in seeded}
    assert after == before


async def test_every_named_query_binds_and_runs(sandbox_connector):
    import datetime as dt
    import uuid
    now = dt.datetime.now(dt.timezone.utc)
    u = uuid.uuid4()
    params = {
        "tower_task_runs": (1, 5), "open_tasks": (["pending"], None, None, 5),
        "operator_questions": (None, None, now, 5), "notification_outbox": (None, now, 5),
        "production_assignments": (None, 5), "assignment": (u,), "assignment_items": (u,),
        "assignment_submissions": (u,), "assignment_stop_choices": (u,),
        "dispatch_holds": (True, None, 5), "judge_queue": ("video", 1, 5),
        "accepted_media": (1, now, 5), "gpu_claims": (True, 5),
        "governed_executions": (u, "x", now, 5), "execution_refusals": (u, now, 5),
        "video_submissions": (u, "x", now, 5),
    }
    assert set(params) == set(tower.NAMED_QUERIES)
    for name, p in params.items():
        assert isinstance(await tower.run_query(name, *p), list), name
