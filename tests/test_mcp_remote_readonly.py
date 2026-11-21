"""The remote read-only MCP surface (#3760): no writes, no unauthenticated bytes."""

from __future__ import annotations

import contextlib
import json
import logging
import os
import stat

import httpx
import pytest

from ai_team_sync.mcp import remote_readonly as rr

TOKEN = "t" * 48

# Every stdio ats-mcp tool that changes state, or acts AS a session. If any of
# these ever appears on the remote transport, the surface is no longer
# read-only. Listed by hand on purpose: deriving it would let a new mutating
# tool slip through unnoticed. test_every_stdio_tool_is_classified below forces
# a decision for each tool added to ats-mcp later.
MUTATING_TOOLS = frozenset({
    "start_session", "complete_session", "pause_session", "resume_session",
    "extend_scope", "delete_lock", "check_locks",  # check_locks may create a lock row
    "delegate", "reconcile_delegation",
    "log_decision", "record_restart",
    "request_override", "respond_to_request",
    "send_message", "acknowledge_message", "readdress_message",
    # session-identity reads: they answer as "my" session, which a remote
    # reader does not have.
    "message_inbox", "message_status", "my_authority", "check_my_override_requests",
    "check_pending_requests", "get_override_request_details",
    "pre_commit_check", "check_git_changes", "whos_editing", "lock_ownership",
    "recent_file_activities", "recent_restarts", "delegation_status",
})
READ_TOOLS_REVIEWED = frozenset({
    "task_brief", "get_decision_history", "team_status",
    "get_session_details", "list_all_locks", "ats_version",
})
# Reads with side effects downstream: Echo memory search refreshes its Redis
# cache and Qdrant access metadata. Off the remote surface by operator ruling
# (2026-11-21) until Echo search takes a read-only flag.
SIDE_EFFECT_READS = frozenset({"preflight"})


class Recorder(httpx.AsyncBaseTransport):
    """Stub upstream: records every request that passed the guard."""

    def __init__(self):
        self.requests: list[httpx.Request] = []

    async def handle_async_request(self, request):
        self.requests.append(request)
        path = request.url.path
        if path == "/api/brief":
            return httpx.Response(200, json={"rendered": "BRIEF OK"})
        if path == "/api/preflight":
            return httpx.Response(200, json={"disposition": "CLEAR",
                                             "preflight_request_id": "p1",
                                             "recommended_next": "go"})
        if path == "/api/version":
            return httpx.Response(200, json={"component": "ats-rest", "commit": "x"})
        if path.startswith("/api/sessions/"):
            return httpx.Response(200, json={"id": "s1", "agent": "a"})
        return httpx.Response(200, json=[])


@pytest.fixture
def upstream(monkeypatch):
    # Pin the upstream bases: the module reads ATS_SERVER_URL at
    # import, and a test must not inherit them from the shell.
    monkeypatch.setattr(rr, "ATS_URL", "http://127.0.0.1:8400")
    rec = Recorder()
    monkeypatch.setattr(rr, "_inner_transport", rec)
    return rec


@contextlib.asynccontextmanager
async def serve():
    # A context manager, not an async fixture: the manager's task group must be
    # entered and exited in the same task, which pytest-asyncio fixtures are not.
    app = rr.build_app(TOKEN)
    async with app.manager.run():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                     base_url="http://127.0.0.1:8402") as c:
            yield c


def _rpc(method, params=None, id_=1):
    return {"jsonrpc": "2.0", "id": id_, "method": method, "params": params or {}}


HDRS = {"Authorization": f"Bearer {TOKEN}",
        "Accept": "application/json, text/event-stream",
        "Content-Type": "application/json"}


async def _call(http, method, params=None, headers=HDRS):
    return await http.post("/mcp", headers=headers, content=json.dumps(_rpc(method, params)))


# ---- the catalog ---------------------------------------------------------------

async def test_no_mutating_tool_on_the_remote_transport(upstream):
    """THE guard (#3760): tools/list over the real HTTP transport."""
    async with serve() as http:
        r = await _call(http, "tools/list")
        assert r.status_code == 200, r.text
        names = {t["name"] for t in r.json()["result"]["tools"]}
        assert not names & MUTATING_TOOLS, f"mutating tools exposed remotely: {names & MUTATING_TOOLS}"
        assert not names & SIDE_EFFECT_READS
        assert names == READ_TOOLS_REVIEWED


async def test_every_stdio_tool_is_classified():
    """A tool added to ats-mcp must be classified here before this suite passes."""
    from ai_team_sync.mcp.server import list_tools
    stdio = {t.name for t in await list_tools()}
    unclassified = stdio - MUTATING_TOOLS - READ_TOOLS_REVIEWED - SIDE_EFFECT_READS
    assert not unclassified, f"classify these for the remote surface: {unclassified}"
    assert not (MUTATING_TOOLS & READ_TOOLS_REVIEWED)
    assert not (SIDE_EFFECT_READS & READ_TOOLS_REVIEWED)


@pytest.mark.parametrize("name", sorted(MUTATING_TOOLS | SIDE_EFFECT_READS))
async def test_mutating_tool_call_is_refused_without_upstream(upstream, name):
    async with serve() as http:
        r = await _call(http, "tools/call", {"name": name, "arguments": {"session_id": "s1"}})
        assert r.status_code == 200
        result = r.json()["result"]
        assert result["isError"] is True
        assert "Refused" in result["content"][0]["text"]
        assert upstream.requests == []


# ---- bearer gate -----------------------------------------------------------------

@pytest.mark.parametrize("auth", [None, "", "Bearer", f"Bearer {TOKEN}x",
                                  f"Basic {TOKEN}", f"Bearer{TOKEN}",
                                  f"Bearer {TOKEN[:-1]}", f"Token {TOKEN}"])
async def test_requests_without_the_exact_bearer_are_rejected_before_mcp(upstream, auth):
    async with serve() as http:
        h = {k: v for k, v in HDRS.items() if k != "Authorization"}
        if auth is not None:
            h["Authorization"] = auth
        r = await _call(http, "tools/call", {"name": "team_status", "arguments": {}}, headers=h)
        assert r.status_code == 401
        assert upstream.requests == []


async def test_duplicate_authorization_headers_are_rejected(upstream):
    app = rr.build_app(TOKEN)
    sent = []

    async def receive():
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(m):
        sent.append(m)

    scope = {"type": "http", "method": "POST", "path": "/mcp",
             "headers": [(b"authorization", f"Bearer {TOKEN}".encode()),
                         (b"authorization", b"Bearer other")]}
    await app(scope, receive, send)
    assert sent[0]["status"] == 401


async def test_websocket_is_closed_unserved():
    app = rr.build_app(TOKEN)
    sent = []

    async def send(m):
        sent.append(m)

    await app({"type": "websocket", "path": "/mcp", "headers": []}, None, send)
    assert sent == [{"type": "websocket.close", "code": 1008}]


async def test_other_paths_404_even_with_bearer(upstream):
    async with serve() as http:
        r = await http.get("/", headers=HDRS)
        assert r.status_code == 404
        r = await http.post("/api/sessions", headers=HDRS, content=b"{}")
        assert r.status_code == 404
        assert upstream.requests == []


async def test_oversized_body_rejected(upstream):
    async with serve() as http:
        h = dict(HDRS)
        r = await http.post("/mcp", headers=h, content=b"x" * (rr.MAX_BODY_BYTES + 1))
        assert r.status_code == 413
        assert upstream.requests == []


# ---- every read tool only touches allowlisted upstream routes --------------------

READ_CALLS = {
    "task_brief": {"objective": "x", "task_id": 3760},
    "get_decision_history": {"ticket_id": 3760},
    "team_status": {},
    "get_session_details": {"session_id": "s1"},
    "list_all_locks": {},
    "ats_version": {},
}


@pytest.mark.parametrize("name", sorted(READ_CALLS))
async def test_read_tools_use_only_read_routes(upstream, name):
    async with serve() as http:
        r = await _call(http, "tools/call", {"name": name, "arguments": READ_CALLS[name]})
        assert r.status_code == 200, r.text
        assert r.json()["result"]["isError"] is False, r.text
        assert upstream.requests, "read tool made no upstream request"
        for req in upstream.requests:
            assert rr.upstream_allowed(req.method, req.url)
            # never acts as a session
            assert "x-ats-approval-token" not in req.headers
            if req.method == "POST":
                body = json.loads(req.content)
                assert body.get("session_id", "") == ""


async def test_brief_never_names_a_session(upstream):
    async with serve() as http:
        await _call(http, "tools/call", {"name": "task_brief",
                                         "arguments": {"objective": "x", "session_id": "evil"}})
        assert json.loads(upstream.requests[0].content)["session_id"] == ""


async def test_session_id_cannot_traverse_paths(upstream):
    async with serve() as http:
        r = await _call(http, "tools/call", {"name": "get_session_details",
                                             "arguments": {"session_id": "../locks"}})
        assert "must be an ATS session id" in r.text
        assert upstream.requests == []


@pytest.mark.parametrize("method,url", [
    ("POST", "http://127.0.0.1:8400/api/sessions"),
    ("POST", "http://127.0.0.1:8400/api/decisions"),
    ("POST", "http://127.0.0.1:8400/api/locks"),
    ("DELETE", "http://127.0.0.1:8400/api/locks/abc"),
    ("POST", "http://127.0.0.1:8400/api/sessions/s1/complete"),
    ("POST", "http://127.0.0.1:8400/api/sessions/s1/heartbeat"),
    ("PATCH", "http://127.0.0.1:8400/api/sessions/s1"),
    ("GET", "http://127.0.0.1:8400/api/sessions/s1/complete"),
    ("GET", "http://evil.example/api/sessions"),
    ("POST", "http://127.0.0.1:8309/api/brief"),
    ("POST", "http://127.0.0.1:8309/api/preflight"),
    ("POST", "http://127.0.0.1:8309/api/echo/memory/search"),
    ("GET", "http://127.0.0.1:8400/api/messages"),
    ("POST", "http://127.0.0.1:8400/api/restarts"),
    ("POST", "http://127.0.0.1:8400/api/version"),
    ("PUT", "http://127.0.0.1:8400/api/brief"),
    ("POST", "http://127.0.0.1:8400/api/sessions/s1"),
    ("GET", "http://127.0.0.1:8401/api/sessions"),
    ("GET", "https://127.0.0.1:8400/api/sessions"),
])
async def test_guard_refuses_non_read_upstream(monkeypatch, method, url):
    monkeypatch.setattr(rr, "ATS_URL", "http://127.0.0.1:8400")
    rec = Recorder()
    async with httpx.AsyncClient(transport=rr._GuardedTransport(rec)) as c:
        with pytest.raises(rr.UpstreamRefused):
            await c.request(method, url)
    assert rec.requests == []


# ---- startup refusals --------------------------------------------------------------

@pytest.mark.parametrize("host", ["0.0.0.0", "192.168.50.135", "::", "tower", "100.115.223.59"])
def test_non_loopback_bind_refused(host):
    with pytest.raises(SystemExit):
        rr._require_loopback(host)


@pytest.mark.parametrize("host", ["127.0.0.1", "::1"])
def test_loopback_bind_allowed(host):
    rr._require_loopback(host)


def test_token_file_rules(tmp_path):
    f = tmp_path / "tok"
    f.write_text(TOKEN + "\n")
    os.chmod(f, 0o600)
    assert rr.load_token(f) == TOKEN
    os.chmod(f, 0o640)
    with pytest.raises(RuntimeError):
        rr.load_token(f)
    f.write_text("short")
    os.chmod(f, 0o600)
    with pytest.raises(RuntimeError):
        rr.load_token(f)
    with pytest.raises(FileNotFoundError):
        rr.load_token(tmp_path / "missing")


# ---- pinned allowlists (#3760 review r1) -------------------------------------------
# These are literals ON PURPOSE. Checking requests with rr.upstream_allowed would
# let a widened allowlist pass its own test; these fail on any widening.

PINNED_UPSTREAM = {
    ("POST", "ats", "/api/brief"),
    ("GET", "ats", "/api/sessions"),
    ("GET", "ats", r"/api/sessions/[A-Za-z0-9-]{1,64}"),
    ("GET", "ats", "/api/decisions"),
    ("GET", "ats", "/api/locks"),
    ("GET", "ats", "/api/restarts"),
    ("GET", "ats", "/api/version"),
}


def test_upstream_allowlist_is_exactly_the_reviewed_set():
    got = {(m, base, p.pattern) for m, base, p in rr.UPSTREAM_ALLOWLIST}
    assert got == PINNED_UPSTREAM


def test_tool_catalog_is_exactly_the_reviewed_set():
    assert rr.READONLY_TOOL_NAMES == READ_TOOLS_REVIEWED
    assert {t.name for t in rr.READONLY_TOOLS} == READ_TOOLS_REVIEWED


EXPECTED_REQUESTS = {
    "task_brief": [("POST", "127.0.0.1:8400", "/api/brief")],
    "get_decision_history": [("GET", "127.0.0.1:8400", "/api/decisions")],
    "team_status": [("GET", "127.0.0.1:8400", "/api/sessions")],
    "get_session_details": [("GET", "127.0.0.1:8400", "/api/sessions/s1")],
    "list_all_locks": [("GET", "127.0.0.1:8400", "/api/locks")],
    "ats_version": [("GET", "127.0.0.1:8400", "/api/version")],
}


@pytest.mark.parametrize("name", sorted(EXPECTED_REQUESTS))
async def test_each_tool_sends_exactly_its_pinned_requests(upstream, name):
    async with serve() as http:
        r = await _call(http, "tools/call", {"name": name, "arguments": READ_CALLS[name]})
        assert r.json()["result"]["isError"] is False, r.text
    got = [(q.method, f"{q.url.host}:{q.url.port}", q.url.path) for q in upstream.requests]
    assert got == EXPECTED_REQUESTS[name]


async def test_brief_recall_is_forced_off(upstream):
    """Recall is Echo memory search, which writes cache/access metadata (review r2)."""
    async with serve() as http:
        await _call(http, "tools/call", {"name": "task_brief",
                                         "arguments": {"objective": "x", "recall": True}})
    assert json.loads(upstream.requests[0].content)["recall"] is False


def test_nothing_reaches_echo_directly():
    assert {base for _, base, _ in rr.UPSTREAM_ALLOWLIST} == {"ats"}


# ---- availability bounds -------------------------------------------------------------

class Huge(httpx.AsyncBaseTransport):
    async def handle_async_request(self, request):
        async def gen():
            for _ in range(rr.MAX_UPSTREAM_BYTES // 65536 + 2):
                yield b"x" * 65536
        return httpx.Response(200, content=gen())


async def test_oversized_upstream_answer_is_refused(monkeypatch):
    monkeypatch.setattr(rr, "_inner_transport", Huge())
    async with serve() as http:
        r = await _call(http, "tools/call", {"name": "list_all_locks", "arguments": {}})
    result = r.json()["result"]
    assert result["isError"] is True
    assert "UpstreamTooLarge" in result["content"][0]["text"]


def test_tool_output_is_truncated():
    out = rr._text("y" * (rr.MAX_OUTPUT_CHARS + 500))[0].text
    assert len(out) < rr.MAX_OUTPUT_CHARS + 100 and out.endswith("characters]")


def test_run_passes_concurrency_and_size_limits(monkeypatch, tmp_path):
    f = tmp_path / "tok"
    f.write_text(TOKEN)
    os.chmod(f, 0o600)
    monkeypatch.setenv("ATS_REMOTE_MCP_TOKEN_FILE", str(f))
    monkeypatch.delenv("ATS_REMOTE_MCP_HOST", raising=False)
    seen = {}
    import uvicorn
    monkeypatch.setattr(uvicorn, "run", lambda app, **kw: seen.update(kw))
    # run() configures the refusal logger; keep that from leaking into other tests.
    monkeypatch.setattr(rr.auth_log, "handlers", [])
    monkeypatch.setattr(rr.auth_log, "propagate", True)
    monkeypatch.setattr(rr.auth_log, "level", rr.auth_log.level)
    rr.run()
    assert rr.auth_log.handlers and rr.auth_log.propagate is False
    assert seen["host"] == "127.0.0.1"
    assert seen["limit_concurrency"] == rr.MAX_CONCURRENCY
    assert seen["h11_max_incomplete_event_size"] == rr.MAX_BODY_BYTES
    assert seen["proxy_headers"] is False


# ---- review r2: encoded bodies and error mapping ----------------------------------

class Gzipped(httpx.AsyncBaseTransport):
    """A 5 KB gzip body that decodes past the cap (the r2 bypass)."""

    def __init__(self):
        self.seen_accept_encoding = []

    async def handle_async_request(self, request):
        import gzip
        self.seen_accept_encoding.append(request.headers.get("accept-encoding"))
        body = gzip.compress(b"[" + b'"' + b"z" * (rr.MAX_UPSTREAM_BYTES * 2) + b'"]')
        return httpx.Response(200, headers={"content-encoding": "gzip",
                                            "content-type": "application/json"},
                              content=body)


@pytest.mark.parametrize("name", ["list_all_locks", "task_brief", "ats_version", "team_status"])
async def test_encoded_upstream_answer_is_refused_as_error(monkeypatch, name):
    gz = Gzipped()
    monkeypatch.setattr(rr, "_inner_transport", gz)
    async with serve() as http:
        r = await _call(http, "tools/call", {"name": name, "arguments": READ_CALLS[name]})
    result = r.json()["result"]
    assert result["isError"] is True, r.text
    assert gz.seen_accept_encoding and set(gz.seen_accept_encoding) == {"identity"}


@pytest.mark.parametrize("name", ["task_brief", "ats_version", "list_all_locks"])
async def test_oversized_answer_is_an_error_for_every_tool(monkeypatch, name):
    monkeypatch.setattr(rr, "_inner_transport", Huge())
    async with serve() as http:
        r = await _call(http, "tools/call", {"name": name, "arguments": READ_CALLS[name]})
    assert r.json()["result"]["isError"] is True, r.text


# ---- RFC 7235 parsing + refusal diagnostics (#3760 claude.ai connector 401s) ------
# A hex-shaped secret on purpose: it survives naive "address characters only"
# filters, which is exactly the leak review r1 of this change demonstrated.
HEX = "0123456789abcdef" * 4


@contextlib.asynccontextmanager
async def serve_hex():
    app = rr.build_app(HEX)
    async with app.manager.run():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                     base_url="http://127.0.0.1:8402") as c:
            yield c


def _h(auth=None, **extra):
    h = {k: v for k, v in HDRS.items() if k != "Authorization"}
    if auth is not None:
        h["Authorization"] = auth
    h.update(extra)
    return h


@pytest.mark.parametrize("auth", [f"Bearer {HEX}", f"bearer {HEX}", f"BEARER {HEX}",
                                  f"  Bearer   {HEX}  ", f"\tBearer {HEX}\t"])
async def test_rfc7235_scheme_case_and_whitespace_accepted(upstream, auth):
    async with serve_hex() as http:
        r = await _call(http, "tools/list", headers=_h(auth))
    assert r.status_code == 200, (auth, r.text)


@pytest.mark.parametrize("auth", [f"Bearer\t{HEX}", f"Bearer \t{HEX}", f"Bearer\x0b{HEX}",
                                  f"Bearer\x0c{HEX}", f"Bearer {HEX[:32]} {HEX[32:]}",
                                  f"Bearer {HEX},Bearer {HEX}", f"Bearer {HEX[1:]}",
                                  f"Bearer {HEX}0", f"Basic {HEX}", "Bearer ", "Bearer",
                                  f"{HEX}0", HEX[1:], f"{HEX[:32]} {HEX[32:]}",
                                  f"{HEX}\t{HEX}", f" {HEX} x"])
async def test_non_rfc_separators_and_near_misses_rejected(upstream, auth):
    async with serve_hex() as http:
        r = await _call(http, "tools/list", headers=_h(auth))
    assert r.status_code == 401, auth
    assert upstream.requests == []


async def _raw(app, method, path, headers):
    sent = []

    async def receive():
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(m):
        sent.append(m)

    await app({"type": "http", "method": method, "path": path,
               "headers": [(k.lower().encode(), v.encode("latin-1")) for k, v in headers]},
              receive, send)
    return sent[0]["status"]


def _refusal_lines(caplog):
    return [r.getMessage() for r in caplog.records if r.name == "ats_remote_mcp.auth"]


def _assert_no_secret(line):
    for i in range(0, len(HEX) - 7):
        assert HEX[i:i + 8] not in line, (HEX[i:i + 8], line)


@pytest.mark.parametrize("auth,scheme,cred_len", [
    (None, "-", 0),
    ("", "<empty>", 0),
    (HEX[:-1], "<none>", 0),
    (f"Basic {HEX}", "basic", 0),
    (f"Weird{HEX[:3]} {HEX}", "<other>", 0),
    (f"Bearer {HEX}0", "bearer", len(HEX) + 1),
    (f"bearer  {HEX[:-2]} ", "bearer", len(HEX) - 2),
    (f"Bearer\t{HEX}", "<other>", 0),
])
async def test_refusal_is_logged_without_the_credential(upstream, caplog, auth, scheme, cred_len):
    caplog.set_level(logging.INFO, logger="ats_remote_mcp.auth")
    async with serve_hex() as http:
        r = await _call(http, "tools/list", headers=_h(
            auth, **{"X-Forwarded-For": "160.79.104.7", "Tailscale-Funnel-Request": "?1"}))
    assert r.status_code == 401
    lines = _refusal_lines(caplog)
    assert len(lines) == 1, lines
    line = lines[0]
    _assert_no_secret(line)
    assert f"auth_headers={0 if auth is None else 1} value_len={0 if auth is None else len(auth)} " in line
    assert f"scheme={scheme} " in line
    assert f"credential_len={cred_len} expected_len={len(HEX)}" in line
    assert "xff=anthropic " in line and "funnel=yes" in line
    assert line.startswith("401 POST /mcp ")


def _secret_groups_valid_ipv6(secret):
    """A VALID IPv6 address built from the secret's hex (review r2 probe)."""
    return ":".join(secret[i:i + 4] for i in range(0, 32, 4))


@pytest.mark.parametrize("xff", [HEX, f"1.2.3.4, {HEX}", f"::1%{HEX}", f"{HEX[:4]}::{HEX[4:8]}x",
                                 "1.2.3.4\nWARNING:ats_remote_mcp.auth:forged", "9" * 500,
                                 _secret_groups_valid_ipv6(HEX),
                                 f"::ffff:{HEX[:4]}:{HEX[4:8]}",
                                 f"1.2.3.4, {_secret_groups_valid_ipv6(HEX)}"])
async def test_forwarded_header_never_echoes_client_text(caplog, xff):
    caplog.set_level(logging.INFO, logger="ats_remote_mcp.auth")
    status = await _raw(rr.build_app(HEX), "POST", "/mcp", [("X-Forwarded-For", xff)])
    assert status == 401
    line = _refusal_lines(caplog)[0]
    _assert_no_secret(line)
    assert "\n" not in line and "forged" not in line and len(line) < 300
    xff_field = line.split("xff=")[1].split(" ")[0]
    # Classes only: no digit or hex group from the client survives.
    assert set(xff_field.split(",")) <= {"<invalid>", "v4", "v6", "anthropic", "-"}, xff_field
    assert not any(ch.isdigit() for ch in xff_field.replace("v4", "").replace("v6", ""))


@pytest.mark.parametrize("xff,expected", [
    ("160.79.104.7", "anthropic"), ("160.79.111.255", "anthropic"),
    ("::ffff:160.79.104.7", "anthropic"), ("160.79.112.1", "v4"),
    ("2001:db8::1", "v6"), ("garbage", "<invalid>"),
    ("160.79.104.7, 10.0.0.1", "anthropic,v4"),
    ("160.79.103.255", "v4"), ("160.79.104.0", "anthropic"),
    ("153.61.208.0", "anthropic"), ("153.61.223.255", "anthropic"),
    ("153.61.207.255", "v4"), ("153.61.224.0", "v4"),
    ("::ffff:153.61.210.9", "anthropic"),
])
def test_forwarded_address_classification(xff, expected):
    assert rr._client_addresses(xff) == expected


@pytest.mark.parametrize("method,path", [
    ("POST", f"/{HEX}"), ("POST", "/mcp\nWARNING:ats_remote_mcp.auth:forged"),
    ("POST", f"/mcp/{HEX}"), (HEX[:16], "/mcp"), ("GET", "/mcp\r\nx"),
])
async def test_path_and_method_never_echoed(caplog, method, path):
    caplog.set_level(logging.INFO, logger="ats_remote_mcp.auth")
    status = await _raw(rr.build_app(HEX), method, path, [])
    assert status == 401
    line = _refusal_lines(caplog)[0]
    _assert_no_secret(line)
    assert "\n" not in line and "\r" not in line and "forged" not in line
    assert line.split(" ")[1] in ("POST", "GET", "<other>")
    assert line.split(" ")[2] in ("/mcp", "<other>")


async def test_success_is_not_logged_as_refusal(upstream, caplog):
    caplog.set_level(logging.INFO, logger="ats_remote_mcp.auth")
    async with serve_hex() as http:
        r = await _call(http, "tools/list", headers=_h(f"Bearer {HEX}"))
    assert r.status_code == 200 and _refusal_lines(caplog) == []


async def test_duplicate_headers_logged_as_refused(caplog):
    caplog.set_level(logging.INFO, logger="ats_remote_mcp.auth")
    status = await _raw(rr.build_app(HEX), "POST", "/mcp",
                        [("Authorization", f"Bearer {HEX}"), ("Authorization", f"Bearer {HEX}")])
    assert status == 401
    line = _refusal_lines(caplog)[0]
    assert "auth_headers=2" in line
    _assert_no_secret(line)


# ---- bare secret as the whole Authorization value (claude.ai connector) ----------

@pytest.mark.parametrize("auth", [HEX, f"  {HEX}  ", f"\t{HEX}"])
async def test_bare_secret_accepted(upstream, auth):
    async with serve_hex() as http:
        r = await _call(http, "tools/list", headers=_h(auth))
    assert r.status_code == 200, (auth, r.text)


async def test_bare_wrong_secret_logs_value_len_not_value(upstream, caplog):
    caplog.set_level(logging.INFO, logger="ats_remote_mcp.auth")
    wrong = "f" * len(HEX)
    async with serve_hex() as http:
        r = await _call(http, "tools/list", headers=_h(wrong))
    assert r.status_code == 401
    line = _refusal_lines(caplog)[0]
    assert f"value_len={len(HEX)} scheme=<none> credential_len=0" in line
    assert "ffffffff" not in line
