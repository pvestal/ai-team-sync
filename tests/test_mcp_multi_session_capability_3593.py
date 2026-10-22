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


def test_the_capability_file_is_private_and_written_atomically(state):
    sp.save_approval_token("sess-a", "tok-a", cid=CID)
    sp.save_approval_token("sess-b", "tok-b", cid=CID)
    path = state / f"{sp.APPROVAL_TOKEN_PREFIX}{CID[:8]}"
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    leftovers = [p.name for p in state.iterdir()
                 if p.name.startswith(f".{path.name}") or p.name.endswith(".tmp")]
    assert leftovers == [], leftovers
    json.loads(path.read_text())   # a complete document, never a torn write


def test_a_legacy_single_entry_file_is_still_read(state):
    """A process still running pre-#3593 code writes the old shape."""
    path = state / f"{sp.APPROVAL_TOKEN_PREFIX}{CID[:8]}"
    path.write_text(json.dumps({"session_id": "old", "token": "tok-old"}))
    assert sp.load_approval_token("old", cid=CID) == "tok-old"
    sp.save_approval_token("new", "tok-new", cid=CID)
    assert sp.load_approval_token("old", cid=CID) == "tok-old"
    assert sp.load_approval_token("new", cid=CID) == "tok-new"


def test_old_readers_still_find_the_most_recent_capability(state):
    """Pre-#3593 readers parse {'session_id','token'}; keep that pair current."""
    sp.save_approval_token("sess-a", "tok-a", cid=CID)
    sp.save_approval_token("sess-b", "tok-b", cid=CID)
    data = json.loads((state / f"{sp.APPROVAL_TOKEN_PREFIX}{CID[:8]}").read_text())
    assert (data["session_id"], data["token"]) == ("sess-b", "tok-b")


def test_completing_a_session_prunes_only_its_entry(state):
    sp.save_approval_token("sess-a", "tok-a", cid=CID)
    sp.save_approval_token("sess-b", "tok-b", cid=CID)
    sp.forget_approval_token("sess-b", cid=CID)
    assert sp.load_approval_token("sess-b", cid=CID) == ""
    assert sp.load_approval_token("sess-a", cid=CID) == "tok-a"
    data = json.loads((state / f"{sp.APPROVAL_TOKEN_PREFIX}{CID[:8]}").read_text())
    assert (data["session_id"], data["token"]) == ("sess-a", "tok-a")
    sp.forget_approval_token("sess-a", cid=CID)
    assert not (state / f"{sp.APPROVAL_TOKEN_PREFIX}{CID[:8]}").exists()


def test_the_map_is_bounded(state):
    for i in range(sp.APPROVAL_TOKEN_MAX + 10):
        sp.save_approval_token(f"sess-{i}", f"tok-{i}", cid=CID)
    data = json.loads((state / f"{sp.APPROVAL_TOKEN_PREFIX}{CID[:8]}").read_text())
    assert len(data["sessions"]) == sp.APPROVAL_TOKEN_MAX
    newest = sp.APPROVAL_TOKEN_MAX + 9
    assert sp.load_approval_token(f"sess-{newest}", cid=CID) == f"tok-{newest}"
    assert sp.load_approval_token("sess-0", cid=CID) == ""


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
    monkeypatch.setattr(mcp, "_IN_PROCESS_TOKENS", {}, raising=False)
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
