"""#3593: one MCP process, several sessions, each keeps its own capability.

session_pointer.save_approval_token kept ONE capability per client id (written
with O_TRUNC) and mcp/server.py kept ONE in-process token, so a second
start_session in the same MCP process overwrote the first session's
capability. Observed 2026-10-15: claude-code 7ff8070e opened 111febd0 (#3591
owner, ATS repo), then 9dfde553 (Echo contributor) for two-repository work;
111febd0 could no longer be completed ("Session capability missing for
completion.") and was reaped. The same overwrite hit four parallel subagents
of one Claude session on 2026-10-22: all of them share one MCP process.

The guard stays exactly as strict: a capability authorizes only its own row,
and a process that never opened a session cannot finalize it.
"""
from __future__ import annotations

import json
import os
import stat
import uuid

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

import ai_team_sync.briefs as briefs
import ai_team_sync.mcp.server as mcp
from ai_team_sync import session_pointer as sp
from ai_team_sync.database import get_db
from ai_team_sync.server import create_app
from tests.test_hook_delivery_live import live_ats  # noqa: F401  (fixture)


# ── the per-cid capability map ───────────────────────────────────────────────

@pytest.fixture
def state(tmp_path, monkeypatch):
    d = tmp_path / "state"
    d.mkdir()
    monkeypatch.setenv("ATS_STATE_DIR", str(d))
    monkeypatch.delenv("ATS_DELEGATION", raising=False)
    return d


CID = "7ff8070e-1111-4222-8333-444444444444"


def test_two_sessions_under_one_client_keep_both_capabilities(state):
    sp.save_approval_token("sess-a", "tok-a", cid=CID)
    sp.save_approval_token("sess-b", "tok-b", cid=CID)
    assert sp.load_approval_token("sess-a", cid=CID) == "tok-a"
    assert sp.load_approval_token("sess-b", cid=CID) == "tok-b"
    assert sp.load_approval_token("sess-c", cid=CID) == ""


def test_the_keyring_is_private_and_written_atomically(state):
    sp.save_approval_token("sess-a", "tok-a", cid=CID)
    sp.save_approval_token("sess-b", "tok-b", cid=CID)
    d = state / sp.CAPABILITY_DIR_NAME
    path = sp.capability_store_path(CID)
    assert path.parent == d and path.name == f"{CID}.json"   # the FULL client id
    assert stat.S_IMODE(d.stat().st_mode) == 0o700
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    leftovers = [p.name for p in d.iterdir() if p.name.endswith(".tmp")]
    assert leftovers == [], leftovers
    data = json.loads(path.read_text())   # a complete document, never a torn write
    assert data["version"] == 2 and data["client_id"] == CID
    assert {sid: e["state"] for sid, e in data["sessions"].items()} == {
        "sess-a": "open", "sess-b": "open"}


def test_a_legacy_single_entry_file_is_still_read_and_never_written(state):
    """A process still running pre-#3593 code writes the old shape, at the old
    eight-character name. It is read for exactly the session it names, and
    never rewritten by the new code."""
    path = state / f"{sp.APPROVAL_TOKEN_PREFIX}{CID[:8]}"
    path.write_text(json.dumps({"session_id": "old", "token": "tok-old"}))
    before = path.read_text()
    assert sp.load_approval_token("old", cid=CID) == "tok-old"
    sp.save_approval_token("new", "tok-new", cid=CID)
    assert sp.load_approval_token("old", cid=CID) == "tok-old"
    assert sp.load_approval_token("new", cid=CID) == "tok-new"
    assert path.read_text() == before


def test_another_clients_file_never_answers(state):
    sp.save_approval_token("sess-a", "tok-a", cid=CID)
    assert sp.load_approval_token("sess-a", cid="9dfde553-0000-4000-8000-000000000000") == ""


# ── the 2026-10-15 sequence through the real MCP handlers ────────────────────

def _envelope(tid):
    from tests.test_task_brief import TASK_ENVELOPE

    return dict(TASK_ENVELOPE, id=int(tid), task_key=f"task-{tid}",
                title=f"Synthetic task {tid}", relations={})


@pytest.fixture
def wired(db_engine, monkeypatch, tmp_path):
    app = create_app()
    factory = async_sessionmaker(db_engine, class_=AsyncSession, expire_on_commit=False)

    async def override_get_db():
        async with factory() as s:
            yield s

    app.dependency_overrides[get_db] = override_get_db
    transport = ASGITransport(app=app)

    class _ASGIClient:
        def __init__(self, *a, **k):
            self._c = AsyncClient(transport=transport, base_url="http://localhost:8400")

        async def __aenter__(self):
            return self._c

        async def __aexit__(self, *exc):
            await self._c.aclose()

    monkeypatch.setattr(mcp.httpx, "AsyncClient", _ASGIClient)
    monkeypatch.setattr(briefs, "fetch_tower_task_data",
                        lambda tid, **kw: (_envelope(tid), None))
    monkeypatch.setattr(briefs, "fetch_task_overlaps",
                        lambda objective, **kw: ([], "Echo stub: not consulted"))
    monkeypatch.setattr(briefs, "recall_memories", lambda *a, **kw: [])
    # Identity this test depends on, declared here (AGENTS.md "Test validity").
    monkeypatch.setenv("ATS_AGENT", "claude-code")
    for var in ("ATS_DELEGATION", "ATS_SESSION_ID", "ATS_SESSION", "ATS_APPROVAL_TOKEN"):
        monkeypatch.delenv(var, raising=False)
    _fresh_process(monkeypatch, tmp_path, "7ff8070e-0000-4000-8000-000000000001")
    return transport


def _fresh_process(monkeypatch, tmp_path, cid):
    """A new MCP process: no in-process sessions, its own client id and state."""
    monkeypatch.setattr(mcp, "_IN_PROCESS_SESSION_ID", None)
    monkeypatch.setattr(mcp, "_IN_PROCESS_TOKENS", {})
    monkeypatch.setattr(mcp, "_IN_PROCESS_STATE", {})
    monkeypatch.setattr(mcp, "_IN_PROCESS_PARENT", {})
    monkeypatch.setattr(mcp, "_CAPABILITY_PROBLEMS", {})
    d = tmp_path / f"state-{uuid.uuid4().hex[:6]}"
    d.mkdir()
    monkeypatch.setenv("ATS_STATE_DIR", str(d))
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", cid)
    monkeypatch.setenv("CLAUDE_PID", str(os.getpid()))


async def _start(ticket, repo_root, description):
    text = (await mcp.call_tool("start_session", {
        "description": description, "scope": [f"src/t{ticket}.py"],
        "repo_root": repo_root, "ticket_id": ticket}))[0].text
    assert "Session started" in text, text
    return text.split("Session ID: ")[1].split()[0]


async def _row(transport, sid):
    async with AsyncClient(transport=transport, base_url="http://localhost:8400") as c:
        return (await c.get(f"/api/sessions/{sid}")).json()


async def _handoffs(transport, ticket):
    async with AsyncClient(transport=transport, base_url="http://localhost:8400") as c:
        r = await c.get(f"/api/tickets/{ticket}/handoffs")
        return r.json() if r.status_code == 200 else []


async def _two_sessions(transport, tmp_path):
    ats_repo, echo_repo = tmp_path / "ai-team-sync", tmp_path / "tower-echo-brain"
    ats_repo.mkdir()
    echo_repo.mkdir()
    a = await _start(3591, str(ats_repo), "#3591 owner, ATS repository")
    b = await _start(4242, str(echo_repo), "Echo-side work for the same change")
    return a, b


async def _use(transport, sid, ticket):
    """Locks were taken at start; record a decision AS this exact session."""
    text = (await mcp.call_tool("log_decision", {
        "session_id": sid, "title": f"choice for {ticket}", "chosen": "x",
        "reasoning": "y"}))[0].text
    assert "Decision logged" in text, text
    async with AsyncClient(transport=transport, base_url="http://localhost:8400") as c:
        locks = [lk for lk in (await c.get("/api/locks")).json() if lk["session_id"] == sid]
        titles = [d["title"] for d in
                  (await c.get("/api/decisions", params={"session_id": sid})).json()]
    assert locks, f"session {sid} holds no lock"
    assert f"choice for {ticket}" in titles, f"the decision was not recorded on {sid}: {titles}"


async def _complete(sid, ticket):
    return (await mcp.call_tool("complete_session", {
        "session_id": sid, "summary": f"done with #{ticket}",
        "handoff": {"verdict": f"verdict for #{ticket}"}}))[0].text


@pytest.mark.asyncio
@pytest.mark.parametrize("with_handoff", [False, True])
async def test_regression_2026_10_15_open_two_close_the_second_then_the_first(
        wired, tmp_path, with_handoff):
    """The observed sequence, using only arguments that existed on 2026-10-15:
    open 111febd0 (#3591), open 9dfde553, complete 9dfde553, then complete
    111febd0 by its explicit id. On main the last call answered
    'Session capability missing for completion.' (or, with a handoff, 'A
    handoff must come from this MCP process's exact active session.') and the
    session was left to the reaper."""
    a, b = await _two_sessions(wired, tmp_path)
    for sid, ticket in ((b, 4242), (a, 3591)):
        args = {"session_id": sid, "summary": f"done with #{ticket}"}
        if with_handoff:
            args["handoff"] = {"verdict": f"verdict for #{ticket}"}
        text = (await mcp.call_tool("complete_session", args))[0].text
        assert "Session completed" in text, text
        row = await _row(wired, sid)
        assert row["status"] == "completed" and not row.get("auto_completed"), row


@pytest.mark.asyncio
@pytest.mark.parametrize("order", ["first_opened_first", "last_opened_first"])
async def test_the_2026_10_15_sequence_closes_both_sessions_with_their_own_handoffs(
        wired, tmp_path, order):
    a, b = await _two_sessions(wired, tmp_path)
    await _use(wired, a, 3591)
    await _use(wired, b, 4242)

    plan = [(a, 3591), (b, 4242)]
    if order == "last_opened_first":
        plan.reverse()
    for sid, ticket in plan:
        text = await _complete(sid, ticket)
        assert "Session completed" in text, text
        assert sid in text
    for sid, ticket in plan:
        row = await _row(wired, sid)
        assert row["status"] == "completed" and not row.get("auto_completed"), row
        verdicts = [h.get("verdict") for h in await _handoffs(wired, ticket)]
        assert f"verdict for #{ticket}" in verdicts, verdicts


@pytest.mark.asyncio
async def test_pause_and_resume_target_the_named_session(wired, tmp_path):
    a, b = await _two_sessions(wired, tmp_path)
    text = (await mcp.call_tool("pause_session", {"session_id": a}))[0].text
    assert "paused" in text and a in text, text
    assert (await _row(wired, a))["status"] == "paused"
    assert (await _row(wired, b))["status"] == "active"
    text = (await mcp.call_tool("resume_session", {"session_id": a}))[0].text
    assert "resumed" in text and a in text, text
    assert (await _row(wired, a))["status"] == "active"


@pytest.mark.asyncio
async def test_without_a_session_id_the_most_recent_session_is_still_the_default(
        wired, tmp_path):
    a, b = await _two_sessions(wired, tmp_path)
    text = (await mcp.call_tool("complete_session", {"summary": "legacy path"}))[0].text
    assert "Session completed" in text and b in text, text
    assert (await _row(wired, a))["status"] == "active"
    # ...and the earlier session becomes the default again, still closable.
    text = (await mcp.call_tool("complete_session", {"summary": "legacy path"}))[0].text
    assert "Session completed" in text and a in text, text


@pytest.mark.asyncio
async def test_a_third_process_cannot_finalize_either_session(wired, tmp_path, monkeypatch):
    a, b = await _two_sessions(wired, tmp_path)
    _fresh_process(monkeypatch, tmp_path, "0badf00d-0000-4000-8000-000000000003")
    for sid in (a, b):
        for tool, args in (("complete_session", {"session_id": sid, "summary": "x"}),
                           ("pause_session", {"session_id": sid}),
                           ("resume_session", {"session_id": sid}),
                           ("log_decision", {"session_id": sid, "title": "t",
                                             "chosen": "c", "reasoning": "r"}),
                           ("extend_scope", {"session_id": sid, "patterns": ["docs/**"]})):
            text = (await mcp.call_tool(tool, args))[0].text
            assert text.startswith("❌"), (tool, text)
        assert (await _row(wired, sid))["status"] == "active"


@pytest.mark.asyncio
async def test_a_third_process_with_the_same_label_but_no_capability_cannot_finalize(
        wired, tmp_path, monkeypatch):
    """Same Claude client id (so the same agent label), but a different process
    with no in-process sessions and its own state directory: the label alone
    is not the capability."""
    a, _b = await _two_sessions(wired, tmp_path)
    _fresh_process(monkeypatch, tmp_path, "7ff8070e-0000-4000-8000-000000000001")
    text = (await mcp.call_tool("complete_session", {"session_id": a, "summary": "x"}))[0].text
    assert "capability missing" in text, text
    assert (await _row(wired, a))["status"] == "active"


@pytest.mark.asyncio
async def test_a_session_id_this_process_did_not_open_is_refused_for_use(
        wired, tmp_path, monkeypatch):
    async with AsyncClient(transport=wired, base_url="http://localhost:8400") as c:
        r = await c.post("/api/sessions", json={
            "developer": "dev", "agent": "claude-code:7ff8070e", "scope": [],
            "description": "someone else's row"})
        foreign = r.json()["id"]
    await _two_sessions(wired, tmp_path)
    text = (await mcp.call_tool("log_decision", {
        "session_id": foreign, "title": "t", "chosen": "c", "reasoning": "r"}))[0].text
    assert text.startswith("❌") and foreign in text, text


# ── a COMPLETED sender keeps its readdress authority (Codex review round 1) ──
#
# routers/messages.readdress_message lets the original sender move an unread
# direct message after turnover "even if that sender session has since
# completed but its capability remains available"
# (tests/test_message_turnover_2907.py). Pruning the capability on completion
# took that away from every MCP and CLI sender.

async def _rest_session(transport, agent):
    async with AsyncClient(transport=transport, base_url="http://localhost:8400") as c:
        r = await c.post("/api/sessions", json={
            "developer": "dev", "agent": agent, "scope": [], "description": agent})
        assert r.status_code == 201, r.text
        return r.json()["id"], r.headers["X-ATS-Approval-Token"]


async def _end(transport, sid, token):
    async with AsyncClient(transport=transport, base_url="http://localhost:8400") as c:
        r = await c.patch(f"/api/sessions/{sid}", json={"status": "completed"},
                          headers={"X-ATS-Approval-Token": token})
        assert r.status_code == 200, r.text


async def _send_and_turn_over(transport, sender):
    """sender (the default session) -> old; then old ends and next appears."""
    old, old_token = await _rest_session(transport, "claude-code:old")
    text = (await mcp.call_tool("send_message", {
        "recipient_session_id": old, "body": "direct"}))[0].text
    assert "Message queued" in text, text
    mid = text.split("Message ID: ")[1].split(".")[0].strip()
    text = (await mcp.call_tool("complete_session", {
        "session_id": sender, "summary": "sent and done"}))[0].text
    assert "Session completed" in text, text
    await _end(transport, old, old_token)
    nxt, _ = await _rest_session(transport, "claude-code:next")
    return mid, old, nxt


@pytest.mark.asyncio
async def test_a_completed_mcp_sender_readdresses_as_the_default_session(wired, tmp_path):
    """One session: after completing it, it is still this process's session."""
    repo = tmp_path / "repo"
    repo.mkdir()
    sender = await _start(3591, str(repo), "sender")
    mid, old, nxt = await _send_and_turn_over(wired, sender)
    text = (await mcp.call_tool("readdress_message", {
        "message_id": mid, "recipient_session_id": nxt}))[0].text
    assert "readdressed" in text and old in text and nxt in text, text


@pytest.mark.asyncio
async def test_a_completed_mcp_sender_readdresses_by_name_beside_an_open_session(
        wired, tmp_path):
    """Two sessions: the open one becomes the default, and the completed sender
    is named with session_id. Its capability is still held."""
    other_repo, repo = tmp_path / "other", tmp_path / "repo"
    other_repo.mkdir()
    repo.mkdir()
    other = await _start(4242, str(other_repo), "still working")
    sender = await _start(3591, str(repo), "sender")
    mid, old, nxt = await _send_and_turn_over(wired, sender)
    assert mcp._IN_PROCESS_SESSION_ID == other
    text = (await mcp.call_tool("readdress_message", {
        "session_id": sender, "message_id": mid, "recipient_session_id": nxt}))[0].text
    assert "readdressed" in text and old in text and nxt in text, text
    text = (await mcp.call_tool("message_status", {
        "session_id": sender, "message_id": mid}))[0].text
    assert "queued, unacknowledged" in text, text


def test_a_completed_cli_sender_keeps_the_capability_it_readdresses_with(
        live_ats, tmp_path, monkeypatch):
    """`ats session complete` must leave the capability the CLI's own lookup
    (load_approval_token(sid, cid=sid) or load_approval_token(sid)) finds."""
    from click.testing import CliRunner

    from ai_team_sync.cli import cli

    url, client, _env = live_ats
    state = tmp_path / "cli-state"
    state.mkdir()
    env = {"ATS_SERVER_URL": url, "ATS_STATE_DIR": str(state), "ATS_AGENT": "claude-code",
           "CLAUDE_CODE_SESSION_ID": "c1100000-0000-4000-8000-000000000001",
           "ATS_DEVELOPER": "dev", "ATS_SESSION_ID": None, "ATS_SESSION": None,
           "ATS_DELEGATION": None}
    for key, value in env.items():
        if value is None:
            monkeypatch.delenv(key, raising=False)
        else:
            monkeypatch.setenv(key, value)
    monkeypatch.chdir(tmp_path)
    runner = CliRunner()

    started = runner.invoke(cli, ["session", "start", "-s", "docs/**", "-d", "sender"])
    assert started.exit_code == 0, started.output
    sender = started.output.split("Session started: ")[1].split()[0]

    def token_for(sid):
        return sp.load_approval_token(sid, cid=sid) or sp.load_approval_token(sid)

    def session(agent):
        r = client.post("/api/sessions", json={"developer": "dev", "agent": agent,
                                                "scope": [], "description": agent})
        assert r.status_code == 201, r.text
        return r.json()["id"], r.headers["X-ATS-Approval-Token"]

    old, old_token = session("claude-code:old")
    sent = client.post("/api/messages", json={
        "sender_session_id": sender, "recipient_session_id": old, "body": "direct"},
        headers={"X-ATS-Approval-Token": token_for(sender)})
    assert sent.status_code == 201, sent.text

    done = runner.invoke(cli, ["session", "complete", "--session-id", sender, "-m", "done"])
    assert done.exit_code == 0, done.output
    assert client.patch(f"/api/sessions/{old}", json={"status": "completed"},
                        headers={"X-ATS-Approval-Token": old_token}).status_code == 200
    nxt, _ = session("claude-code:next")

    token = token_for(sender)
    assert token, "the completed CLI sender lost its capability"
    moved = client.post(f"/api/messages/{sent.json()['id']}/readdress", json={
        "sender_session_id": sender, "recipient_session_id": nxt},
        headers={"X-ATS-Approval-Token": token})
    assert moved.status_code == 200, moved.text
    assert moved.json()["original_recipient_session_id"] == old
