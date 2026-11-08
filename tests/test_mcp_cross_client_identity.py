"""Two logical Codex clients sharing one MCP module/process stay isolated.

This is the live failure shape: the hook-published Codex lifecycle id changes
while an older client still owns an ATS session.  Before request-bound caller
state, the shared globals and live-id record made the older client act with the
newer client's label/default/capability.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

import ai_team_sync.mcp.server as mcp
from ai_team_sync import session_pointer as sp
from ai_team_sync.database import get_db
from ai_team_sync.hooks.session_registration import lifecycle_session_key
from ai_team_sync.mcp import caller_context as cc
from ai_team_sync.server import create_app
from tests.test_hook_delivery_live import McpClient, live_ats  # noqa: F401


CID_A = "019a5f42-1111-7000-8000-000000000001"
CID_B = "019a5f42-2222-7000-8000-000000000002"


@pytest.fixture
def wired_clients(db_engine, monkeypatch, tmp_path):
    app = create_app()
    factory = async_sessionmaker(db_engine, class_=AsyncSession,
                                 expire_on_commit=False)

    async def override_get_db():
        async with factory() as session:
            yield session

    app.dependency_overrides[get_db] = override_get_db
    transport = ASGITransport(app=app)

    class _ASGIClient:
        def __init__(self, *args, **kwargs):
            self._client = AsyncClient(
                transport=transport, base_url="http://localhost:8400")

        async def __aenter__(self):
            return self._client

        async def __aexit__(self, *exc):
            await self._client.aclose()

    monkeypatch.setattr(mcp.httpx, "AsyncClient", _ASGIClient)
    monkeypatch.setenv("ATS_AGENT", "codex")
    monkeypatch.setenv("ATS_STATE_DIR", str(tmp_path / "state"))
    for key in ("ATS_DELEGATION", "ATS_SESSION_ID", "ATS_SESSION",
                "ATS_APPROVAL_TOKEN", "CLAUDE_CODE_SESSION_ID"):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setattr(mcp, "_IN_PROCESS_SESSION_ID", None)
    monkeypatch.setattr(mcp, "_IN_PROCESS_TOKENS", {})
    monkeypatch.setattr(mcp, "_IN_PROCESS_STATE", {})
    monkeypatch.setattr(mcp, "_IN_PROCESS_PARENT", {})
    monkeypatch.setattr(mcp, "_CAPABILITY_PROBLEMS", {})
    cc.reset_context_registry_for_tests()
    return transport


async def _call(context, name, arguments):
    with cc.bind_caller(context):
        return (await mcp.call_tool(name, arguments))[0].text


async def _start(context, root, lane):
    text = await _call(context, "start_session", {
        "scope": [lane], "description": f"caller {lane}",
        "repo_root": root})
    assert "Session started" in text, text
    return text.split("Session ID: ", 1)[1].split()[0]


async def _rows(transport):
    async with AsyncClient(transport=transport,
                           base_url="http://localhost:8400") as client:
        sessions = (await client.get("/api/sessions")).json()
        locks = (await client.get("/api/locks")).json()
    return sessions, locks


async def _row(transport, session_id):
    async with AsyncClient(transport=transport,
                           base_url="http://localhost:8400") as client:
        return (await client.get(f"/api/sessions/{session_id}")).json()


def test_request_metadata_selects_isolated_buckets_on_one_transport():
    transport = SimpleNamespace()
    server = SimpleNamespace(request_context=SimpleNamespace(
        session=transport, meta={"threadId": CID_A, "sessionId": "host-session-0001"}))
    a = cc.context_for_request(server, "codex")
    server.request_context = SimpleNamespace(
        session=transport, meta={"threadId": CID_B, "sessionId": "host-session-0001"})
    b = cc.context_for_request(server, "codex")
    server.request_context = SimpleNamespace(
        session=transport, meta={"threadId": CID_A, "sessionId": "host-session-0001"})

    assert cc.context_for_request(server, "codex") is a
    assert a is not b
    assert a.client_id != b.client_id
    assert a.agent_label != b.agent_label


@pytest.mark.parametrize("meta", [
    {"codex/thread_id": "bad identity"},
    {"codex/thread_id": "short"},
    {"codex/thread_id": CID_A, "thread_id": CID_B},
    {"threadId": CID_A, "sessionId": "bad identity"},
    {"sessionId": "host-session-0001"},
])
def test_ambiguous_or_malformed_request_identity_fails_closed(meta):
    server = SimpleNamespace(request_context=SimpleNamespace(
        session=SimpleNamespace(), meta=meta))
    with pytest.raises(cc.CallerContextError, match="refusing"):
        cc.context_for_request(server, "codex")


def test_one_thread_cannot_change_its_host_session_binding():
    transport = SimpleNamespace()
    server = SimpleNamespace(request_context=SimpleNamespace(
        session=transport, meta={"threadId": CID_A, "sessionId": "host-session-0001"}))
    cc.context_for_request(server, "codex")
    server.request_context = SimpleNamespace(
        session=transport, meta={"threadId": CID_A, "sessionId": "host-session-0002"})
    with pytest.raises(cc.CallerContextError, match="changed host session"):
        cc.context_for_request(server, "codex")


def test_metadata_free_codex_transport_snapshots_one_hook_identity(monkeypatch):
    cc.reset_context_registry_for_tests()
    current = {"cid": lifecycle_session_key("codex", CID_A)}
    monkeypatch.setattr(sp, "claude_session_id", lambda: current["cid"])
    transport = SimpleNamespace()
    server = SimpleNamespace(request_context=SimpleNamespace(
        session=transport, meta=None))

    context = cc.context_for_request(server, "codex")
    assert context.source == "legacy_hook"
    assert context.client_id == lifecycle_session_key("codex", CID_A)
    current["cid"] = lifecycle_session_key("codex", CID_B)
    with pytest.raises(cc.CallerContextError, match="changed inside one MCP transport"):
        cc.context_for_request(server, "codex")


def test_two_metadata_free_transports_fail_closed(monkeypatch):
    cc.reset_context_registry_for_tests()
    monkeypatch.setattr(sp, "claude_session_id", lambda: CID_A)
    first = SimpleNamespace(request_context=SimpleNamespace(
        session=SimpleNamespace(), meta=None))
    second = SimpleNamespace(request_context=SimpleNamespace(
        session=SimpleNamespace(), meta=None))
    cc.context_for_request(first, "codex")
    with pytest.raises(cc.CallerContextError, match="multiple metadata-free"):
        cc.context_for_request(second, "codex")


def test_real_stdio_process_interleaves_two_codex_thread_contexts(
        live_ats, tmp_path):
    """Exercise the actual MCP SDK RequestContext and Codex `_meta` boundary."""

    url, http, base_env = live_ats
    state = tmp_path / "stdio-state"
    state.mkdir()
    env = dict(base_env, ATS_STATE_DIR=str(state), ATS_SERVER_URL=url,
               ATS_AGENT="codex")
    for key in ("ATS_DELEGATION", "ATS_SESSION_ID", "ATS_SESSION",
                "ATS_APPROVAL_TOKEN", "CLAUDE_CODE_SESSION_ID"):
        env.pop(key, None)
    client = McpClient(env)
    host_session = "codex-host-session-0001"

    def tool(thread_id, name, arguments=None):
        result = client.request("tools/call", {
            "name": name,
            "arguments": arguments or {},
            "_meta": {"threadId": thread_id, "sessionId": host_session},
        })
        return "\n".join(part.get("text", "") for part in result["content"])

    def started(thread_id, lane):
        text = tool(thread_id, "start_session", {
            "scope": [lane], "description": f"stdio {lane}",
            "repo_root": str(tmp_path)})
        assert "Session ID:" in text, text
        return text.split("Session ID: ", 1)[1].split()[0]

    try:
        sid_a = started(CID_A, "a/**")
        sid_b = started(CID_B, "b/**")
        sent = tool(CID_A, "send_message", {
            "recipient_session_id": sid_b, "body": "stdio A to B"})
        message_id = sent.split("Message ID: ", 1)[1].split(".", 1)[0]
        assert "stdio A to B" in tool(CID_B, "message_inbox")
        assert "acknowledged" in tool(CID_B, "acknowledge_message", {
            "message_id": message_id})

        refused = tool(CID_A, "extend_scope", {
            "session_id": sid_b, "patterns": ["stolen/**"]})
        assert refused.startswith("❌ Refusing to act as session"), refused

        done_a = tool(CID_A, "complete_session", {
            "session_id": sid_a, "summary": "stdio A done"})
        done_b = tool(CID_B, "complete_session", {
            "session_id": sid_b, "summary": "stdio B done"})
        assert "Session completed" in done_a and sid_a in done_a
        assert "Session completed" in done_b and sid_b in done_b
        row_a = http.get(f"/api/sessions/{sid_a}").json()
        row_b = http.get(f"/api/sessions/{sid_b}").json()
        assert row_a["status"] == row_b["status"] == "completed"
        assert row_a["agent"] == f"codex:{lifecycle_session_key('codex', CID_A)[:8]}"
        assert row_b["agent"] == f"codex:{lifecycle_session_key('codex', CID_B)[:8]}"
    finally:
        client.close()


@pytest.mark.asyncio
async def test_two_concurrent_codex_clients_never_inherit_identity_or_authority(
        wired_clients, tmp_path):
    context_a = cc.isolated_caller("codex", CID_A)
    context_b = cc.isolated_caller("codex", CID_B)
    root = tmp_path / "repo"
    root.mkdir()
    a_started = asyncio.Event()
    b_started = asyncio.Event()
    b_checked_a_lock = asyncio.Event()
    a_completed = asyncio.Event()
    shared: dict[str, str] = {}

    async def client_a():
        shared["a"] = await _start(context_a, str(root), "src/a/**")
        a_started.set()
        await b_started.wait()

        # Reproduce the live process record being overwritten by client B.
        sp.publish_live_cid(context_b.client_id)

        # Any tool call heartbeats only the bound caller, even while another
        # task is live in this same module/process.
        before_a = await _row(wired_clients, shared["a"])
        before_b = await _row(wired_clients, shared["b"])
        await asyncio.sleep(0.01)
        details = await _call(context_a, "get_session_details", {})
        assert shared["a"] in details, details
        after_a = await _row(wired_clients, shared["a"])
        after_b = await _row(wired_clients, shared["b"])
        assert before_a["last_heartbeat"] is None
        assert after_a["last_heartbeat"] is not None
        assert after_b["last_heartbeat"] == before_b["last_heartbeat"]

        text = await _call(context_a, "extend_scope", {
            "patterns": ["tests/a/**"]})
        assert "Scope extended" in text, text
        text = await _call(context_a, "send_message", {
            "recipient_session_id": shared["b"], "body": "from A"})
        assert "Message queued" in text, text
        shared["message"] = text.split("Message ID: ", 1)[1].split(".", 1)[0]

        # A target id is not authority: A cannot become B for an update.
        text = await _call(context_a, "extend_scope", {
            "session_id": shared["b"], "patterns": ["stolen/**"]})
        assert text.startswith("❌ Refusing to act as session"), text
        await b_checked_a_lock.wait()
        completed = await _call(context_a, "complete_session", {
            "session_id": shared["a"], "summary": "A done"})
        a_completed.set()
        return completed

    async def client_b():
        await a_started.wait()
        shared["b"] = await _start(context_b, str(root), "src/b/**")
        b_started.set()
        while "message" not in shared:
            await asyncio.sleep(0)

        inbox = await _call(context_b, "message_inbox", {})
        assert "from A" in inbox and shared["a"] in inbox, inbox
        ack = await _call(context_b, "acknowledge_message", {
            "message_id": shared["message"]})
        assert "acknowledged" in ack, ack

        # B cannot release A's lock merely by naming A's session.
        _sessions, locks = await _rows(wired_clients)
        a_lock = next(lock for lock in locks
                      if lock["session_id"] == shared["a"])
        refused = await _call(context_b, "delete_lock", {
            "session_id": shared["a"], "lock_id": a_lock["id"]})
        assert refused.startswith("❌ Refusing to act as session"), refused
        b_checked_a_lock.set()
        await a_completed.wait()
        return await _call(context_b, "complete_session", {
            "session_id": shared["b"], "summary": "B done"})

    done_a, done_b = await asyncio.gather(client_a(), client_b())
    assert "Session completed" in done_a and shared["a"] in done_a
    assert "Session completed" in done_b and shared["b"] in done_b
    assert context_a.default_session_id == shared["a"]
    assert context_b.default_session_id == shared["b"]
    assert set(context_a.tokens) == {shared["a"]}
    assert set(context_b.tokens) == {shared["b"]}

    sessions, locks = await _rows(wired_clients)
    by_id = {row["id"]: row for row in sessions}
    assert by_id[shared["a"]]["agent"] == context_a.agent_label
    assert by_id[shared["b"]]["agent"] == context_b.agent_label
    assert by_id[shared["a"]]["status"] == "completed", (done_a, done_b, by_id)
    assert by_id[shared["b"]]["status"] == "completed", (done_a, done_b, by_id)
    assert not [lock for lock in locks
                if lock["session_id"] in {shared["a"], shared["b"]}]


@pytest.mark.asyncio
async def test_wrong_client_cannot_acknowledge_another_clients_message(
        wired_clients, tmp_path):
    a = cc.isolated_caller("codex", CID_A)
    b = cc.isolated_caller("codex", CID_B)
    root = tmp_path / "repo-ack"
    root.mkdir()
    sid_a = await _start(a, str(root), "a/**")
    sid_b = await _start(b, str(root), "b/**")
    sent = await _call(a, "send_message", {
        "recipient_session_id": sid_b, "body": "B only"})
    mid = sent.split("Message ID: ", 1)[1].split(".", 1)[0]

    wrong = await _call(a, "acknowledge_message", {"message_id": mid})
    assert wrong.startswith("❌ Acknowledgement failed"), wrong
    assert "acknowledged" in await _call(b, "acknowledge_message", {
        "message_id": mid})
    assert a.default_session_id == sid_a and b.default_session_id == sid_b


@pytest.mark.asyncio
async def test_metadata_and_default_id_without_capability_cannot_heartbeat_or_mutate(
        wired_clients):
    context = cc.isolated_caller("codex", CID_A)
    async with AsyncClient(transport=wired_clients,
                           base_url="http://localhost:8400") as client:
        made = await client.post("/api/sessions", json={
            "developer": "dev", "agent": context.agent_label,
            "scope": [], "description": "no caller capability"})
        foreign = made.json()["id"]
    context.default_session_id = foreign

    status = await _call(context, "team_status", {})
    assert "active session" in status
    row = await _row(wired_clients, foreign)
    assert row["last_heartbeat"] is None

    refused = await _call(context, "request_override", {
        "pattern": "src/**", "justification": "must not dispatch"})
    assert "capability missing for mutation" in refused.lower(), refused
    assert (await _row(wired_clients, foreign))["last_heartbeat"] is None
