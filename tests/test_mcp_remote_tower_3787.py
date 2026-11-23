"""#3787 Phase 1: read-only Tower tools on the remote MCP surface.

Pure tests (no database, no network): the catalog, the exact SQL and upstream
requests each tool can produce, argument refusals, redaction, the role
self-check, the fixed service-state probes, per-client credentials, rate
limits and the audit log. tests/test_remote_read_sql_3787.py runs the SQL and
the role against a private PostgreSQL sandbox.
"""

from __future__ import annotations

import contextlib
import json
import logging
import os
import re
import uuid
from pathlib import Path

import httpx
import pytest

from ai_team_sync.mcp import remote_controls as rc
from ai_team_sync.mcp import remote_readonly as rr
from ai_team_sync.mcp import remote_tower as tower

TOKEN = "t" * 48
OTHER = "o" * 48
SHOT = "11111111-2222-3333-4444-555555555555"


# ---- fakes -------------------------------------------------------------------------

class Upstream(httpx.AsyncBaseTransport):
    def __init__(self):
        self.requests: list[httpx.Request] = []

    async def handle_async_request(self, request):
        self.requests.append(request)
        if request.url.path.startswith("/api/tower-tasks/"):
            return httpx.Response(200, json={
                "id": 3760, "title": "t", "description": "see src/x.py and /opt/anime-studio/a.py",
                "notes": "Authorization: Bearer abcdefghijklmnop12345", "task_context": {}})
        if request.url.path == "/health":
            return httpx.Response(200, json={"status": "healthy", "loaded_revision": "abc1234",
                                             "database_user": "patrick", "service": "echo"})
        if request.url.path == "/api/version":
            return httpx.Response(200, json={"component": "ats-rest", "commit": "46a8098",
                                             "package_path": "/home/x", "pid": 1})
        return httpx.Response(200, json=[])


def good_role_row(db_key="anime"):
    return {"who": tower.DB_ROLE, "elevated": False, "memberships": 0, "outside": 0,
            "writable": 0, "create_schemas": 0, "replication": False, "sequences": 0,
            "writer_functions": 0, "readable_views": sorted(tower.EXPECTED_VIEWS[db_key]),
            "ro": "on"}


class FakeConn:
    def __init__(self, log, role_row=None, rows=None, db_key="anime"):
        self.log, self.rows = log, rows or []
        self.role_row = role_row or good_role_row(db_key)

    @contextlib.asynccontextmanager
    async def transaction(self, **kw):
        self.log.append(("tx", kw))
        yield

    async def fetchrow(self, sql, *p):
        self.log.append(("role_check", sql))
        return self.role_row

    async def fetch(self, sql, *p):
        self.log.append(("fetch", sql, p))
        return self.rows

    async def close(self):
        pass


@pytest.fixture
def env(monkeypatch, tmp_path):
    monkeypatch.setattr(rr, "ATS_URL", "http://127.0.0.1:8400")
    monkeypatch.setattr(rr, "ECHO_URL", "http://127.0.0.1:8309")
    up = Upstream()
    monkeypatch.setattr(rr, "_inner_transport", up)
    log: list = []
    state = {"log": log, "up": up, "conns": [], "role_row": None, "rows": []}

    async def connect(db_key):
        state["conns"].append(db_key)
        return FakeConn(log, state["role_row"], state["rows"], db_key)

    monkeypatch.setattr(tower, "connect", connect)
    monkeypatch.setattr(tower, "_verified", set())
    monkeypatch.setattr(rr, "RATE_LIMITER", rc.RateLimiter(per_minute={"call": 1000, "db": 1000}))
    audit = rc.AuditLog(tmp_path / "audit.jsonl")
    monkeypatch.setattr(rr, "AUDIT", audit)
    state["audit_path"] = tmp_path / "audit.jsonl"
    return state


@contextlib.asynccontextmanager
async def serve(clients=TOKEN):
    app = rr.build_app(clients)
    async with app.manager.run():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                     base_url="http://127.0.0.1:8402") as c:
            yield c


def _hdr(token=TOKEN):
    return {"Authorization": f"Bearer {token}", "Accept": "application/json, text/event-stream",
            "Content-Type": "application/json"}


async def call(http, name, args=None, token=TOKEN):
    r = await http.post("/mcp", headers=_hdr(token), content=json.dumps(
        {"jsonrpc": "2.0", "id": 1, "method": "tools/call",
         "params": {"name": name, "arguments": args or {}}}))
    assert r.status_code == 200, r.text
    return r.json()["result"]


def text(result):
    return result["content"][0]["text"]


# ---- catalog --------------------------------------------------------------------------

async def test_every_tool_is_annotated_read_only(env):
    async with serve() as http:
        r = await http.post("/mcp", headers=_hdr(), content=json.dumps(
            {"jsonrpc": "2.0", "id": 1, "method": "tools/list"}))
    tools = r.json()["result"]["tools"]
    assert len(tools) == 19
    for t in tools:
        ann = t.get("annotations") or {}
        assert ann.get("readOnlyHint") is True, t["name"]
        assert ann.get("destructiveHint") is False, t["name"]
        assert ann.get("openWorldHint") is False, t["name"]


def test_no_tool_accepts_a_path_url_sql_or_command_argument():
    banned = re.compile(r"(?i)^(path|file|filename|url|uri|sql|query|command|cmd|argv|"
                        r"host|database|db|table|column|where|order_by)$")
    for t in tower.TOWER_TOOLS:
        for prop in t.inputSchema.get("properties", {}):
            assert not banned.match(prop), (t.name, prop)


# ---- SQL: pinned, literal, remote_read only ---------------------------------------------

def test_every_named_query_is_a_select_on_remote_read_views_only():
    for name, (db, sql) in tower.NAMED_QUERIES.items():
        assert db in tower.DATABASES, name
        flat = " ".join(sql.split())
        assert flat.upper().startswith("SELECT "), name
        assert ";" not in flat and "{" not in flat and "%" not in flat, name
        rels = re.findall(r"(?i)\b(?:from|join)\s+([A-Za-z_.]+)", flat)
        assert rels and all(r.startswith("remote_read.") for r in rels), (name, rels)
        assert not re.search(r"(?i)\b(insert|update|delete|truncate|alter|create|drop|grant|"
                             r"copy|call|do|set|lock|vacuum|pg_read|dblink|lo_)\b", flat), name


def test_named_query_set_is_pinned():
    assert set(tower.NAMED_QUERIES) == {
        "tower_task_runs", "open_tasks", "operator_questions", "notification_outbox",
        "production_assignments", "assignment", "assignment_items",
        "assignment_submissions", "assignment_stop_choices", "dispatch_holds",
        "judge_queue", "accepted_media", "gpu_claims", "governed_executions",
        "execution_refusals", "video_submissions"}


CALLS = {
    "tower_task_runs": ({"task_id": 3760}, ["tower_task_runs"]),
    "open_tasks": ({"status": "pending", "gate": "decision", "project_id": 17}, ["open_tasks"]),
    "operator_questions": ({"task_id": 3760, "since": "2026-11-01T00:00:00Z"},
                           ["operator_questions"]),
    "notification_outbox": ({"status": "delivered"}, ["notification_outbox"]),
    "production_assignments": ({}, ["production_assignments"]),
    "dispatch_holds": ({"active_only": False}, ["dispatch_holds"]),
    "judge_queue": ({"lane": "video"}, ["judge_queue"]),
    "accepted_media": ({"project_id": 3}, ["accepted_media"]),
    "gpu_claims": ({}, ["gpu_claims"]),
    "governed_executions": ({"shot_id": SHOT}, ["governed_executions", "execution_refusals"]),
    "video_submissions": ({"shot_id": SHOT, "outcome": "failed"}, ["video_submissions"]),
}


@pytest.mark.parametrize("name", sorted(CALLS))
async def test_each_db_tool_sends_only_its_named_queries_in_read_only_transactions(env, name):
    args, queries = CALLS[name]
    async with serve() as http:
        res = await call(http, name, args)
    assert res["isError"] is False, text(res)
    fetched = [e[1] for e in env["log"] if e[0] == "fetch"]
    assert fetched == [tower.NAMED_QUERIES[q][1] for q in queries]
    txs = [e[1] for e in env["log"] if e[0] == "tx"]
    assert txs and all(kw.get("readonly") is True for kw in txs)
    assert env["up"].requests == []  # no HTTP upstream at all


async def test_assignment_detail_reads_items_submissions_and_stop_choices(env):
    env["rows"] = [{"id": SHOT}]
    async with serve() as http:
        res = await call(http, "production_assignments", {"assignment_id": SHOT})
    assert res["isError"] is False
    fetched = [e[1] for e in env["log"] if e[0] == "fetch"]
    assert fetched == [tower.NAMED_QUERIES[q][1] for q in
                       ("assignment", "assignment_items", "assignment_submissions",
                        "assignment_stop_choices")]
    params = [e[2] for e in env["log"] if e[0] == "fetch"]
    assert all(p == (uuid.UUID(SHOT),) for p in params)


@pytest.mark.parametrize("name,args", [
    ("open_tasks", {"status": "pending' OR '1'='1"}),
    ("open_tasks", {"status": "completed"}),          # not an open status
    ("open_tasks", {"gate": "x"}),
    ("judge_queue", {"lane": "video; DROP TABLE shots"}),
    ("operator_questions", {"state": "Answered"}),     # not a lowercase word
    ("operator_questions", {"since": "yesterday"}),
    ("governed_executions", {"shot_id": "../../etc/passwd"}),
    ("tower_task_runs", {"task_id": "3760"}),          # string, not int
    ("tower_task_runs", {"task_id": True}),
    ("tower_task_runs", {"task_id": 0}),
    ("tower_task_runs", {}),
    ("dispatch_holds", {"active_only": "no"}),
    ("gpu_claims", {"limit": -1}),
    ("tower_task", {"task_id": "3760/resolve"}),
])
async def test_bad_arguments_are_refused_before_any_query(env, name, args):
    async with serve() as http:
        res = await call(http, name, args)
    assert res["isError"] is True
    assert not [e for e in env["log"] if e[0] == "fetch"]
    assert env["up"].requests == []


async def test_limits_over_the_schema_are_refused_and_handlers_cap_too(env):
    # The MCP SDK validates arguments against inputSchema (maximum: 100) first...
    async with serve() as http:
        res = await call(http, "open_tasks", {"limit": 10_000})
    assert res["isError"] is True and not [e for e in env["log"] if e[0] == "fetch"]
    # ...and the handler caps independently of the schema.
    assert tower._limit({"limit": 10_000}, 100) == 100


# ---- the role self-check ------------------------------------------------------------------

@pytest.mark.parametrize("bad", [
    {"who": "patrick"}, {"elevated": True}, {"memberships": 1}, {"outside": 3},
    {"writable": 1}, {"create_schemas": 1}, {"ro": "off"},
    # review r1: replication, sequences, state-writing functions (large objects,
    # SECURITY DEFINER), and any readable remote_read object beyond the reviewed set
    {"replication": True}, {"sequences": 1}, {"writer_functions": 1},
    {"readable_views": sorted(tower.EXPECTED_VIEWS["anime"] | {"secret_dump"})},
    {"readable_views": sorted(tower.EXPECTED_VIEWS["anime"] - {"gpu_claims"})}])
async def test_a_role_that_can_do_more_than_read_remote_read_is_refused(env, bad):
    env["role_row"] = {**good_role_row("anime"), **bad}
    async with serve() as http:
        res = await call(http, "gpu_claims")
    assert res["isError"] is True and "self-check" in text(res)
    assert not [e for e in env["log"] if e[0] == "fetch"]


async def test_missing_credential_reports_unavailable_without_connecting(env, monkeypatch, tmp_path):
    monkeypatch.setattr(tower, "connect", tower._connect)
    monkeypatch.setattr(tower, "DB_FILE", tmp_path / "absent.json")
    async with serve() as http:
        res = await call(http, "judge_queue")
    assert res["isError"] is True and "Unavailable" in text(res)


@pytest.mark.parametrize("cfg,mode", [
    ({"user": "patrick", "password": "x", "host": "127.0.0.1"}, 0o600),
    ({"user": "tower_remote_ro", "password": "x", "host": "192.168.50.135"}, 0o600),
    ({"user": "tower_remote_ro", "password": "x", "host": "db.example.com"}, 0o600),
    ({"user": "tower_remote_ro", "password": "", "host": "127.0.0.1"}, 0o600),
    ({"user": "tower_remote_ro", "password": "x", "host": "127.0.0.1"}, 0o644),
])
def test_db_config_refusals(tmp_path, cfg, mode):
    p = tmp_path / "db.json"
    p.write_text(json.dumps(cfg))
    os.chmod(p, mode)
    with pytest.raises(tower.NotConfigured):
        tower.load_db_config(p)


def test_db_config_accepts_the_dedicated_role_on_loopback(tmp_path):
    p = tmp_path / "db.json"
    p.write_text(json.dumps({"user": "tower_remote_ro", "password": "x", "host": "127.0.0.1"}))
    os.chmod(p, 0o600)
    assert tower.load_db_config(p)["user"] == "tower_remote_ro"


# ---- Echo upstream ------------------------------------------------------------------------

async def test_tower_task_sends_exactly_one_get_to_echo_and_scrubs_secrets(env):
    async with serve() as http:
        res = await call(http, "tower_task", {"task_id": 3760})
    assert res["isError"] is False
    got = [(q.method, f"{q.url.host}:{q.url.port}", q.url.path) for q in env["up"].requests]
    assert got == [("GET", "127.0.0.1:8309", "/api/tower-tasks/3760")]
    out = text(res)
    assert "abcdefghijklmnop12345" not in out and "<redacted>" in out
    # repo citations kept as repo-relative labels; the absolute path is not echoed
    assert "src/x.py" in out and "anime-studio:a.py" in out and "/opt/anime-studio" not in out


@pytest.mark.parametrize("method,url", [
    ("GET", "http://127.0.0.1:8309/api/tower-tasks/0"),
    ("GET", "http://127.0.0.1:8309/api/tower-tasks/1/runs"),
    ("POST", "http://127.0.0.1:8309/api/tower-tasks/1"),
    ("POST", "http://127.0.0.1:8309/api/tower-tasks/resolve"),
    ("GET", "http://127.0.0.1:8309/api/echo/system/logs"),
    ("GET", "http://127.0.0.1:8309/api/echo/integrations/google"),
    ("GET", "http://127.0.0.1:8309/api/vault/keys"),
    ("GET", "http://127.0.0.1:8309/api/operator-questions/1"),
    ("POST", "http://127.0.0.1:8309/api/echo/memory/search"),
    ("POST", "http://127.0.0.1:8309/mcp"),
    ("GET", "http://127.0.0.1:8401/api/scenes/review-queue"),
    ("GET", "http://127.0.0.1:8401/api/system/gpu/arbiter"),
    ("GET", "http://127.0.0.1:8401/api/system/health"),
    ("GET", "http://127.0.0.1:8401/health"),
])
async def test_guard_refuses_everything_else_on_echo_and_all_of_anime_studio(env, method, url):
    async with rr._client() as c:
        with pytest.raises(rr.UpstreamRefused):
            await c.request(method, url)
    assert env["up"].requests == []


# ---- redaction ------------------------------------------------------------------------------

@pytest.mark.parametrize("secret", [
    "Bearer abcdefghijklmnopqrstuvwx",
    "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0.abcdefghijklmnopqrs",
    "sk-abcdefghijklmnopqrstuvwxyz",
    "ghp_abcdefghijklmnopqrstuvwxyz0123",
    "hvs.CAESIabcdefghijklmnopqrstuvwxyz",
    "password=hunter2hunter2",
    "token: 0123456789abcdef",
])
async def test_production_rows_are_scrubbed_of_credentials_and_paths(env, secret):
    env["rows"] = [{"reason": f"failed: {secret} at /opt/anime-studio/output/x.mp4",
                    "keyframe_sha": "a" * 64}]
    async with serve() as http:
        res = await call(http, "dispatch_holds")
    out = text(res)
    assert secret not in out and "<redacted>" in out
    # the path is withheld either as <path> or inside a credential value that
    # runs to the end of the line (r5: unquoted values are never cut at a space)
    assert "/opt/anime-studio" not in out and "output/x.mp4" not in out
    assert "a" * 64 in out  # hashes are evidence, never redacted


@pytest.mark.parametrize("value,leak", [
    ({"stop_conditions": {"token": "supersecretvalue123456"}}, "supersecretvalue123456"),
    ({"context": {"telegram_chat_id": 123456789}}, "123456789"),
    ({"scope": {"nested": [{"api_key": "k-0123456789"}]}}, "k-0123456789"),
    ({"reason": 'body {"token": "jsonsecret0001", "x": 1}'}, "jsonsecret0001"),
    ({"reason": '{"chat_id": 987654321}'}, "987654321"),
    ({"reason": "see /home/patrick/private/job.log"}, "/home/patrick/private"),
    ({"reason": "see /proc/self/environ"}, "/proc/self"),
])
def test_scrub_is_key_aware_and_covers_json_text_and_paths(value, leak):
    for paths in (True, False):
        out = json.dumps(tower.scrub(value, paths=paths))
        assert leak not in out, (paths, out)


def test_echo_text_keeps_repo_citations_but_not_other_paths():
    s = tower.scrub("fix /opt/tower-echo-brain/src/core/x.py; log /home/patrick/private/a.log; "
                    "escape /opt/anime-studio/../../etc/passwd", paths=False)
    assert "echo:src/core/x.py" in s
    assert "/home/patrick/private" not in s and "etc/passwd" not in s


async def test_task_run_error_paths_are_withheld(env):
    env["rows"] = [{"error": "crashed reading /home/patrick/private/job.log", "id": 1}]
    async with serve() as http:
        res = await call(http, "tower_task_runs", {"task_id": 1})
    assert "/home/patrick/private" not in text(res) and "<path>" in text(res)


# ---- service_state ----------------------------------------------------------------------------

PINNED_COMMANDS = {
    *((f"unit:{u}", ("systemctl", "show", u, "-p", tower.UNIT_PROPS)) for u in tower.SYSTEM_UNITS),
    *((f"user-unit:{u}", ("systemctl", "--user", "show", u, "-p", tower.UNIT_PROPS))
      for u in tower.USER_UNITS),
    ("disk-head:echo", ("git", "-C", "/opt/tower-echo-brain", "rev-parse", "--short=12", "HEAD")),
    ("disk-head:anime_studio", ("git", "-C", "/opt/anime-studio", "rev-parse", "--short=12", "HEAD")),
    ("echo-env", ("systemctl", "show", "tower-echo-brain.service", "-p", "Environment")),
}


def test_fixed_commands_are_pinned():
    assert set(tower.FIXED_COMMANDS) == PINNED_COMMANDS
    for _, argv in tower.FIXED_COMMANDS:
        assert argv[0] in ("systemctl", "git")
        assert argv[1:3] != ("start",) and not {"start", "stop", "restart", "enable",
                                                 "disable", "mask", "unmask", "kill"} & set(argv)


async def test_service_state_shows_allowlisted_flags_only(env, monkeypatch, tmp_path):
    seen: list = []

    async def fake_run(argv):
        seen.append(argv)
        if argv[-1] == "Environment":
            return ('Environment="TOWER_EXECUTOR_START_PAUSED=true" ENABLE_SYSTEM_COMMANDS=false '
                    'DB_USER=patrick VAULT_ADDR=http://127.0.0.1:8200 '
                    'AUTONOMY_OVERNIGHT_ONLY=$(curl evil)\n')
        if argv[0] == "git":
            return "776e213d9abc\n"
        return "ActiveState=active\nSubState=running\nUnitFileState=enabled\n" \
               "ActiveEnterTimestamp=Sat 2026-11-21\nExecStart=/secret/path --token=x\n"

    monkeypatch.setattr(tower, "run_fixed", fake_run)
    flags = tmp_path / "flags.env"
    flags.write_text("ANIME_STUDIO_AUTH_MODE=single_user\nPOV_SERVICE_TOKEN_SHA256=deadbeef\n"
                     "ANIME_STUDIO_OPERATOR_REQUEST_TELEGRAM_CHAT_ID=123456\n"
                     "H3_VIDEO_ENABLED='1 && rm'\n")
    monkeypatch.setattr(tower, "AS_FLAGS_FILE", flags)
    async with serve() as http:
        res = await call(http, "service_state")
    assert res["isError"] is False, text(res)
    out = text(res)
    assert set(seen) == {argv for _, argv in tower.FIXED_COMMANDS}
    for leaked in ("deadbeef", "123456", "patrick", "8200", "curl evil", "--token",
                   "/secret/path", "package_path", "database_user", "rm'"):
        assert leaked not in out, leaked
    data = json.loads(out.split("\n", 1)[1])
    assert data["flags"]["anime_studio"]["ANIME_STUDIO_AUTH_MODE"] == "single_user"
    assert data["flags"]["anime_studio"]["H3_VIDEO_ENABLED"] == "<redacted>"
    assert data["flags"]["echo"] == {"AUTONOMY_OVERNIGHT_ONLY": "<redacted>",
                                     "ENABLE_SYSTEM_COMMANDS": "false",
                                     "TOWER_EXECUTOR_START_PAUSED": "true"}
    assert data["echo_health"]["loaded_revision"] == "abc1234"
    assert data["ats"]["commit"] == "46a8098"
    assert data["disk_head"]["anime_studio"] == "776e213d9abc"
    got = {(q.method, q.url.path) for q in env["up"].requests}
    assert got == {("GET", "/health"), ("GET", "/api/version")}


# ---- per-client credentials ---------------------------------------------------------------------

async def test_each_client_has_its_own_secret_and_is_attributed(env):
    async with serve({"claude-ai": TOKEN, "claude-code": OTHER}) as http:
        assert (await call(http, "gpu_claims"))["isError"] is False
        assert (await call(http, "gpu_claims", token=OTHER))["isError"] is False
        bad = await http.post("/mcp", headers=_hdr(OTHER + "x"), content="{}")
        assert bad.status_code == 401
    lines = [json.loads(l) for l in env["audit_path"].read_text().splitlines()]
    assert [l["client"] for l in lines] == ["claude-ai", "claude-code"]


def test_shared_secret_between_clients_is_refused():
    with pytest.raises(ValueError):
        rr.BearerGate(lambda *a: None, {"a": TOKEN, "b": TOKEN})


def _token_file(p: Path, value: str, mode=0o600):
    p.write_text(value)
    os.chmod(p, mode)
    return p


def test_load_clients(tmp_path):
    primary = _token_file(tmp_path / "remote-mcp.token", TOKEN)
    d = tmp_path / "clients.d"
    assert rc.load_clients(primary, "claude-ai", d) == {"claude-ai": TOKEN}  # absent dir
    d.mkdir(mode=0o700)
    _token_file(d / "claude-code.token", OTHER)
    assert rc.load_clients(primary, "claude-ai", d) == {"claude-ai": TOKEN, "claude-code": OTHER}


@pytest.mark.parametrize("case", ["dup_secret", "loose_file", "loose_dir", "bad_name",
                                  "stray_file", "short", "symlink"])
def test_load_clients_refusals(tmp_path, case):
    primary = _token_file(tmp_path / "remote-mcp.token", TOKEN)
    d = tmp_path / "clients.d"
    d.mkdir(mode=0o700)
    if case == "dup_secret":
        _token_file(d / "x.token", TOKEN)
    elif case == "loose_file":
        _token_file(d / "x.token", OTHER, 0o640)
    elif case == "loose_dir":
        os.chmod(d, 0o750)
        _token_file(d / "x.token", OTHER)
    elif case == "bad_name":
        _token_file(d / "X Y.token", OTHER)
    elif case == "stray_file":
        _token_file(d / "notes.txt", OTHER)
    elif case == "short":
        _token_file(d / "x.token", "short")
    elif case == "symlink":
        target = _token_file(tmp_path / "elsewhere", OTHER)
        (d / "x.token").symlink_to(target)
    with pytest.raises(RuntimeError):
        rc.load_clients(primary, "claude-ai", d)


# ---- rate limits -----------------------------------------------------------------------------------

async def test_rate_limit_refuses_without_querying(env, monkeypatch):
    now = [0.0]
    monkeypatch.setattr(rr, "RATE_LIMITER", rc.RateLimiter(
        per_minute={"call": 3, "db": 2}, clock=lambda: now[0]))
    async with serve() as http:
        results = [await call(http, "gpu_claims") for _ in range(3)]
        assert [r["isError"] for r in results] == [False, False, True]
        assert "rate limit" in text(results[2])
        n = len([e for e in env["log"] if e[0] == "fetch"])
        assert n == 2
        now[0] += 60
        assert (await call(http, "gpu_claims"))["isError"] is False


def test_rate_limits_are_per_client():
    rl = rc.RateLimiter(per_minute={"call": 1, "db": 1}, clock=lambda: 0.0)
    assert rl.allow("a", "call") and not rl.allow("a", "call")
    assert rl.allow("b", "call")


# ---- audit log -------------------------------------------------------------------------------------

@pytest.fixture
def journal():
    """What this process writes to the journal, via run()'s own logging setup."""
    import io
    stream = io.StringIO()
    saved = {n: (logging.getLogger(n).handlers[:], logging.getLogger(n).propagate,
                 logging.getLogger(n).level)
             for n in ("ats_remote_mcp.auth", "ats_remote_mcp.audit", "mcp", "uvicorn.error")}
    for n in saved:
        logging.getLogger(n).handlers = []
    rr.configure_logging(stream)
    yield stream
    for n, (handlers, propagate, level) in saved.items():
        lg = logging.getLogger(n)
        lg.handlers, lg.propagate, lg.level = handlers, propagate, level


async def test_audit_never_records_free_text_or_unlisted_names(env, journal):
    async with serve() as http:
        await call(http, "task_brief", {"objective": "SECRET-PHRASE-OBJECTIVE", "task_id": 7})
        await call(http, "start_session_SECRETNAME", {})
        await call(http, "open_tasks", {"status": "pending"})
    raw = env["audit_path"].read_text() + journal.getvalue()
    assert "<message withheld>" in journal.getvalue()  # the SDK's "not listed" warning
    assert "SECRET-PHRASE-OBJECTIVE" not in raw and "SECRETNAME" not in raw
    lines = [json.loads(l) for l in env["audit_path"].read_text().splitlines()]
    assert [l["tool"] for l in lines] == ["task_brief", "<unlisted>", "open_tasks"]
    assert lines[0]["args"]["task_id"] == 7 and lines[0]["args"]["objective"]["len"] > 0
    assert lines[1]["outcome"] == "refused"
    assert lines[2]["args"]["status"] == "pending" and lines[2]["outcome"] == "ok"
    assert all({"client", "ms", "outcome", "ts"} <= set(l) for l in lines)


@pytest.mark.parametrize("tool,key", [("operator_questions", "state"),
                                      ("governed_executions", "outcome"),
                                      ("video_submissions", "outcome"),
                                      ("dispatch_holds", "scope_kind"),
                                      ("production_assignments", "status")])
async def test_free_filter_words_are_hashed_in_the_audit(env, tool, key):
    """review r1: a lowercase word accepted as a filter is still client text."""
    async with serve() as http:
        await call(http, tool, {key: "clientsecret"})
    raw = env["audit_path"].read_text()
    assert "clientsecret" not in raw
    assert json.loads(raw.splitlines()[-1])["args"][key]["len"] > 0


def test_audit_enums_are_the_schema_enums():
    assert tower.AUDIT_ENUMS["open_tasks"] == {"status": frozenset(tower._OPEN),
                                               "gate": tower.GATES}
    assert tower.AUDIT_ENUMS["judge_queue"] == {"lane": tower.LANES}
    assert tower.AUDIT_ENUMS["notification_outbox"] == {"status": tower.OUTBOX_STATUSES}
    assert tower.AUDIT_ENUMS["operator_questions"] == {}


async def test_audit_file_is_private(env):
    async with serve() as http:
        await call(http, "gpu_claims")
    assert oct(env["audit_path"].stat().st_mode & 0o777) == "0o600"


async def test_write_tools_remain_refused_and_audited(env):
    async with serve() as http:
        for name in ("start_session", "log_decision", "update_tower_task",
                     "record_operator_ruling", "trigger_generation", "send_notification"):
            res = await call(http, name, {"x": 1})
            assert res["isError"] is True and "Refused" in text(res)
    assert env["up"].requests == [] and not env["conns"]


# ---- review round 2 regressions ------------------------------------------------------

@pytest.mark.parametrize("value,leak", [
    ("trace /data/private/render.log", "/data/private"),
    ("trace /srv2/x/y.log and /a/b", "/srv2/x"),
    ('body {"token": ["jsonsecret0001"]}', "jsonsecret0001"),
    ('body {"credential": {"value": "jsonsecret0002"}}', "jsonsecret0002"),
    ('[{"x": 1, "api_key": {"k": "jsonsecret0003"}}]', "jsonsecret0003"),
    ('broken {"token": ["jsonsecret0004", ', "jsonsecret0004"),
])
def test_r2_any_absolute_root_and_container_json_secrets_are_withheld(value, leak):
    for paths in (True, False):
        assert leak not in json.dumps(tower.scrub(value, paths=paths)), (paths, value)


def test_r2_urls_and_dates_are_not_mistaken_for_paths():
    s = tower.scrub("see https://tower.local/anime/x and 11/23/2026", paths=True)
    assert "https://tower.local/anime/x" in s and "11/23/2026" in s


async def test_r2_unknown_argument_is_refused_and_never_named_in_the_audit(env):
    async with serve() as http:
        res = await call(http, "gpu_claims", {"clientsecret": 7})
        assert res["isError"] is True          # additionalProperties: false
        await call(http, "task_brief", {"objective": "x", "clientsecret2": 7})
    raw = env["audit_path"].read_text()
    assert "clientsecret" not in raw
    last = json.loads(raw.splitlines()[-1])
    assert last["tool"] == "task_brief" and last["args"]["<unknown_keys>"] == 1


def test_r2_every_tower_tool_schema_forbids_unknown_arguments():
    for t in tower.TOWER_TOOLS:
        assert t.inputSchema.get("additionalProperties") is False, t.name


async def test_r2_unlisted_calls_spend_the_rate_limit_and_refusals_are_throttled(env, monkeypatch):
    now = [0.0]
    monkeypatch.setattr(rr, "RATE_LIMITER", rc.RateLimiter(
        per_minute={"call": 1, "db": 10}, clock=lambda: now[0]))
    async with serve() as http:
        first = await call(http, "no_such_tool")
        later = [await call(http, "no_such_tool") for _ in range(5)]
    assert "not available" in text(first)
    assert all("rate limit" in text(r) for r in later)
    lines = [json.loads(l) for l in env["audit_path"].read_text().splitlines()]
    assert [l["outcome"] for l in lines] == ["refused", "rate_limited"]  # 4 refusals not logged


async def test_r2_tower_output_is_capped_like_every_tool(env):
    env["rows"] = [{"reason": "x" * 900, "id": i} for i in range(200)]
    async with serve() as http:
        res = await call(http, "dispatch_holds", {"limit": 100})
    out = text(res)
    assert len(out) <= rr.MAX_OUTPUT_CHARS + 80 and out.endswith("characters]")


# ---- review round 3 regressions ------------------------------------------------------

@pytest.mark.parametrize("value,leak", [
    ("trace /dataé/private/render.log", "private/render"),
    ("trace /data private/render.log", "private/render"),
    ("clip at /mnt/10TB2/My Videos/scene 3.mp4 failed", "scene 3"),
    ("x /опт/секрет/файл", "секрет"),
    ("\x000\x00 then /home/a/b", "/home/a"),
])
def test_r3_unicode_and_space_paths_are_withheld(value, leak):
    for paths in (True, False):
        out = tower.scrub(value, paths=paths)
        assert leak not in out and "<path>" in out, (paths, out)


def test_r3_echo_text_keeps_routes_and_repo_labels_but_not_paths_after_them():
    s = tower.scrub("GET /api/tower-tasks/3787 then read /opt/tower-echo-brain/src/x.py "
                    "near /home/patrick/secret dir/a.txt", paths=False)
    assert "/api/tower-tasks/3787" in s and "echo:src/x.py" in s
    assert "secret dir" not in s and "/home/patrick" not in s


async def test_r3_malformed_calls_spend_the_limit_and_are_audited(env, monkeypatch):
    now = [0.0]
    monkeypatch.setattr(rr, "RATE_LIMITER", rc.RateLimiter(
        per_minute={"call": 2, "db": 10}, clock=lambda: now[0]))
    async with serve() as http:
        a = await call(http, "gpu_claims", {"clientsecret": 7})
        b = await call(http, "judge_queue", {"lane": "not-a-lane"})
        c = await call(http, "gpu_claims")
    assert a["isError"] and b["isError"] and "input schema" in text(a)
    assert "clientsecret" not in text(a) and "not-a-lane" not in text(b)
    assert "rate limit" in text(c)  # the two malformed calls spent the budget
    lines = [json.loads(l) for l in env["audit_path"].read_text().splitlines()]
    assert [l["outcome"] for l in lines] == ["bad_argument", "bad_argument", "rate_limited"]
    assert "clientsecret" not in env["audit_path"].read_text()


async def test_r3_suppressed_refusals_are_counted_on_the_next_audited_line(env, monkeypatch):
    now = [0.0]
    monkeypatch.setattr(rr, "RATE_LIMITER", rc.RateLimiter(
        per_minute={"call": 1, "db": 10}, clock=lambda: now[0]))
    async with serve() as http:
        await call(http, "gpu_claims")                       # ok
        for _ in range(4):
            await call(http, "gpu_claims")                   # 1 logged + 3 suppressed
        now[0] += 11
        await call(http, "gpu_claims")                       # still limited (1/min): logged
    lines = [json.loads(l) for l in env["audit_path"].read_text().splitlines()]
    assert [l["outcome"] for l in lines] == ["ok", "rate_limited", "rate_limited"]
    assert "suppressed_refusals" not in lines[1] and lines[2]["suppressed_refusals"] == 3


# ---- review round 4 regressions ------------------------------------------------------

@pytest.mark.parametrize("value", [
    "authorization: supersecret123456",
    "Authorization: Basic supersecret123456",
    "payload={'token': 'supersecret123456'}",
    'payload={\\"token\\":\\"supersecret123456\\"}',
    "access_token=supersecret123456&x=1",
    "cookie: session=supersecret123456",
    "{'chat_id': 123456789012}",
])
def test_r4_every_quoting_form_of_a_credential_is_withheld(value):
    leak = "123456789012" if "chat_id" in value else "supersecret123456"
    for paths in (True, False):
        assert leak not in json.dumps(tower.scrub(value, paths=paths)), (paths, value)


async def test_r4_credential_forms_are_withheld_through_a_tool(env):
    env["rows"] = [{"reason": "authorization: supersecret123456 / payload={'token': 'supersecret123456'}"}]
    async with serve() as http:
        res = await call(http, "dispatch_holds")
    assert "supersecret123456" not in text(res)


@pytest.mark.parametrize("attr,url", [
    ("ECHO_URL", "http://127.0.0.1:8401"), ("ATS_URL", "http://127.0.0.1:8401"),
    ("ECHO_URL", "http://192.168.50.135:8309"), ("ATS_URL", "https://127.0.0.1:8400"),
])
def test_r4_a_misconfigured_origin_refuses_everything_and_refuses_startup(monkeypatch, attr, url):
    monkeypatch.setattr(rr, attr, url)
    for method, target in (("GET", url + "/health"), ("GET", url + "/api/tower-tasks/3787"),
                           ("POST", url + "/api/brief"), ("GET", url + "/api/version"),
                           ("GET", "http://127.0.0.1:8309/health"),
                           ("GET", "http://127.0.0.1:8400/api/version")):
        assert rr.upstream_allowed(method, httpx.URL(target)) is False
    with pytest.raises(rr.OriginNotPinned):
        rr.require_pinned_origins()


def test_r4_pinned_origins_pass():
    rr.require_pinned_origins()  # module defaults
    assert rr.PINNED_ORIGINS == {"ats": ("http", "127.0.0.1", 8400),
                                 "echo": ("http", "127.0.0.1", 8309)}


# ---- review round 5 regressions ------------------------------------------------------

PEM = "-----BEGIN OPENSSH PRIVATE KEY-----\nAAAABBBBCCCC\nDDDD\n-----END OPENSSH PRIVATE KEY-----"


@pytest.mark.parametrize("value,leaks", [
    ("password: 'horse battery staple'", ["battery", "staple"]),
    ('passphrase = "horse battery staple" ok', ["battery", "staple"]),
    ('{\\"password\\":\\"horse battery staple\\"}', ["battery", "staple"]),
    ("{'secret': 'a \\' b staple'}", ["staple"]),
    ("password: horse battery staple", ["battery", "staple"]),
    ("token=abc def\\nnext line", ["abc", "def"]),
    (PEM, ["AAAABBBBCCCC", "DDDD"]),
    (PEM.replace("\n", "\\n"), ["AAAABBBBCCCC"]),
    ("key starts -----BEGIN RSA PRIVATE KEY-----\nAAAABBBBCCCC (truncated", ["AAAABBBBCCCC"]),
    ("{'private_key': '" + PEM.replace("\n", "\\n") + "'}", ["AAAABBBBCCCC"]),
])
def test_r5_values_with_spaces_and_private_keys_are_withheld(value, leaks):
    for paths in (True, False):
        out = json.dumps(tower.scrub(value, paths=paths))
        for leak in leaks:
            assert leak not in out, (paths, value, out)


async def test_r5_through_tool(env):
    env["rows"] = [{"reason": "password: 'horse battery staple' " + PEM}]
    async with serve() as http:
        out = text(await call(http, "dispatch_holds"))
    assert "battery" not in out and "AAAABBBBCCCC" not in out


@pytest.mark.parametrize("bad", ["Bearer" + "x" * 40, "bearer" + "y" * 40, "x" * 20 + " " + "y" * 20])
def test_r5_ambiguous_client_secret_shapes_are_refused(tmp_path, bad):
    with pytest.raises(ValueError):
        rr.BearerGate(lambda *a: None, {"a": "z" * 48, "b": bad})
    primary = _token_file(tmp_path / "remote-mcp.token", bad)
    with pytest.raises(RuntimeError):
        rc.load_clients(primary, "claude-ai", None)


async def test_r5_a_credential_matching_two_clients_is_refused_not_first_wins(env, monkeypatch):
    # Bypass the shape check to construct the exact r5 collision and prove the
    # gate itself refuses an ambiguous match instead of attributing by order.
    monkeypatch.setattr(rc, "check_secret_shape", lambda *a, **k: None)
    x = "q" * 48
    for order in ({"pre": "Bearer" + x, "suf": x}, {"suf": x, "pre": "Bearer" + x}):
        async with serve(order) as http:
            r = await http.post("/mcp", headers={**_hdr("Bearer" + x)}, content="{}")
            # "Bearer"+x sent bare matches "pre" (bare) and "suf" (joined form): ambiguous
            r2 = await http.post("/mcp", headers={"Authorization": "Bearer" + x,
                                                  "Accept": "application/json, text/event-stream",
                                                  "Content-Type": "application/json"},
                                 content=json.dumps({"jsonrpc": "2.0", "id": 1,
                                                     "method": "tools/list"}))
        assert r2.status_code == 401


def test_r6_prose_after_a_credential_key_is_withheld_to_end_of_line_by_design():
    # Accepted cost (review r6): no prose exception for any credential key.
    out = tower.scrub("Next requires operator authorization: Codex review\nnext line", paths=False)
    assert out == "Next requires operator authorization: <redacted>\nnext line"


@pytest.mark.parametrize("value,leak", [
    ("authorization: supersecret123456", "supersecret123456"),
    ("Authorization: Basic dXNlcjpwYXNz", "dXNlcjpwYXNz"),
    ("token: 0123456789abcdef", "0123456789abcdef"),
    ("chat_id: 987654321", "987654321"),
    ("nonce=Zx_9-abcdEFGH", "Zx_9-abcdEFGH"),
    ("secret: horse battery staple", "battery"),
])
def test_r5_credential_shaped_values_after_any_key_are_withheld(value, leak):
    for paths in (True, False):
        assert leak not in tower.scrub(value, paths=paths), value


@pytest.mark.parametrize("value,leak", [
    ('{\\"password\\":\\"a\\\\\\" b staple\\"}', "staple"),   # r6 probe
    ('{"password":"a\\" b staple"}', "staple"),
    ("{'secret': 'x \\' y staple'}", "staple"),
    ("access_token: abcdefghijkl", "abcdefghijkl"),
    ("refresh_token: deadbeef", "deadbeef"),
    ("user_id: patrick", "patrick"),
    ("chat_id: opsroom", "opsroom"),
    ("nonce: abcdefghijk", "abcdefghijk"),
])
def test_r6_escaped_closes_and_every_credential_key_withheld(value, leak):
    for paths in (True, False):
        assert leak not in tower.scrub(value, paths=paths), (paths, value)


def test_r6_text_after_a_properly_closed_value_survives():
    out = tower.scrub('{"password": "s3cr et", "shot": "abc"}', paths=True)
    assert "s3cr" not in out and '"shot": "abc"' in out
