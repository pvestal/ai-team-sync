"""#3677: a subagent's start_session never takes its parent's pointer.

Every subagent of one Claude session shares that session's id, its one stdio
MCP process and its per-client pointer (~/.ats_session_<client id>). Observed
2026-11-07 and again 2026-11-15: a subagent's start_session (no
parent_session_id) moved that pointer and the process default to the
subagent's row, so the PARENT's edits and commits were judged as the
subagent's and refused against the parent's own locks; concurrent forks then
had to save_pointer before every commit and raced each other doing it.

What Claude Code itself tells ATS, and this suite drives through the real
entry points:
  * a PreToolUse payload fired inside a subagent carries `agent_id`;
  * an MCP tools/call carries `_meta["claudecode/toolUseId"]`, the same
    `tool_use_id` that hook received.

The REPRODUCER tests use only entry points that exist on main (the MCP
call_tool handler, an MCP request context with tool-use metadata, git
commits, the hook modules run as Claude Code runs them), so the same file shows
them failing on main. Identity is declared per test; every test has its own
ATS_STATE_DIR, and no capability value is printed.
"""
from __future__ import annotations

import io
import json
import os
import subprocess
import sys
import uuid
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest
from httpx import ASGITransport, AsyncClient
from mcp.server.lowlevel import server as lowlevel
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

import ai_team_sync.mcp.server as mcp
from ai_team_sync import session_pointer as sp
from ai_team_sync.database import get_db
from ai_team_sync.hooks import commit_guard as cg
from ai_team_sync.mcp import caller_context as cc
from ai_team_sync.server import create_app
from ai_team_sync.session_marker import AUTOREG_DESCRIPTION
from tests.test_commit_guard_live import _bash_guard, _commit, _git, world  # noqa: F401
from tests.test_hook_delivery_live import live_ats  # noqa: F401  (fixture)

ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / "src"
CID = "c3677c36-0000-4000-8000-000000003677"
AGENT_A = "a3677aaaa0000001"
AGENT_B = "b3677bbbb0000002"


# ── harness: the MCP handlers against the real app, through request context ──

class _Transport:
    """Stands in for the MCP SDK's ServerSession (weak-referenceable)."""


def _declare(monkeypatch, tmp_path):
    monkeypatch.setenv("ATS_AGENT", "claude-code")
    for var in ("ATS_DELEGATION", "ATS_SESSION_ID", "ATS_SESSION", "ATS_APPROVAL_TOKEN"):
        monkeypatch.delenv(var, raising=False)
    state = tmp_path / f"state-{uuid.uuid4().hex[:6]}"
    state.mkdir()
    monkeypatch.setenv("ATS_STATE_DIR", str(state))
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", CID)
    monkeypatch.setenv("CLAUDE_PID", str(os.getpid()))
    monkeypatch.setattr(mcp, "_IN_PROCESS_SESSION_ID", None)
    for name in ("_IN_PROCESS_TOKENS", "_IN_PROCESS_STATE", "_IN_PROCESS_PARENT",
                 "_IN_PROCESS_AGENT", "_CAPABILITY_PROBLEMS"):
        monkeypatch.setattr(mcp, name, {}, raising=False)
    cc.reset_context_registry_for_tests()
    return state


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
    state = _declare(monkeypatch, tmp_path)
    return SimpleNamespace(transport=transport, state=state, mcp_transport=_Transport())


def _hook_records(tool_use_id: str, agent: str | None) -> None:
    """Run the PreToolUse hook exactly as Claude Code would for this call.
    `agent` None = the main thread (Claude Code omits agent_id there). On main
    the module does not exist; the call then records nothing, which is what
    main sees."""
    payload = {"tool_name": "mcp__ai-team-sync__start_session", "session_id": CID,
               "tool_use_id": tool_use_id, "hook_event_name": "PreToolUse",
               "tool_input": {}}
    if agent is not None:
        payload["agent_id"] = agent
    subprocess.run([sys.executable, "-m", "ai_team_sync.hooks.pre_tool_use_ats_caller"],
                   input=json.dumps(payload), env=dict(os.environ, PYTHONPATH=str(SOURCE)),
                   capture_output=True, text=True, timeout=60)


async def _call_as(w, agent: str | None, tool: str, *, record: bool = True, **args) -> str:
    """One tools/call issued by `agent` (None = main thread) of Claude session CID."""
    tool_use_id = f"toolu_{uuid.uuid4().hex}"
    if record:
        _hook_records(tool_use_id, agent)
    token = lowlevel.request_ctx.set(SimpleNamespace(
        session=w.mcp_transport, meta={"claudecode/toolUseId": tool_use_id}))
    try:
        return (await mcp.call_tool(tool, args))[0].text
    finally:
        lowlevel.request_ctx.reset(token)


async def _start_as(w, agent, description, scope, repo, **extra):
    text = await _call_as(w, agent, "start_session", description=description, scope=scope,
                          repo_root=str(repo), **extra)
    assert "Session started" in text, text
    return text.split("Session ID: ")[1].split()[0]


async def _rest(w, method, path, **kw):
    async with AsyncClient(transport=w.transport, base_url="http://localhost:8400") as c:
        return await getattr(c, method)(path, **kw)


async def _row(w, sid):
    return (await _rest(w, "get", f"/api/sessions/{sid}")).json()


async def _decision_titles(w, sid):
    """Decisions this test logged ("by ..."); the server's own admission
    record on every session is not one of them."""
    return [d["title"] for d in (await _rest(
        w, "get", "/api/decisions", params={"session_id": sid})).json()
        if d["title"].startswith("by ")]


def _pointer():
    return sp.resolve_pointer(cid=CID, allow_global=False)


# ── REPRODUCER: the parent keeps its pointer and its default ─────────────────

@pytest.mark.asyncio
async def test_reproducer_subagent_start_session_keeps_parent_pointer_and_default(
        wired, tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    parent = await _start_as(wired, None, "coordinator", ["src/parent.py"], repo)
    assert _pointer() == parent

    child = await _start_as(wired, AGENT_A, "worker A", ["src/a.py"], repo)

    assert child != parent
    assert _pointer() == parent, "the subagent's start_session moved the parent's pointer"
    # The parent's next call without session_id still acts as the parent.
    text = await _call_as(wired, None, "log_decision", title="by the coordinator",
                          chosen="x", reasoning="y")
    assert "Decision logged" in text, text
    assert "by the coordinator" in await _decision_titles(wired, parent)
    assert "by the coordinator" not in await _decision_titles(wired, child)
    assert (await _row(wired, parent))["status"] == "active"


@pytest.mark.asyncio
async def test_reproducer_subagent_never_claims_the_parents_sessionstart_placeholder(
        wired, tmp_path):
    """The parent has only its SessionStart lifecycle placeholder. A subagent's
    start_session used to claim that row in place, taking the parent's
    lifecycle identity with the subagent's scope and description."""
    made = await _rest(wired, "post", "/api/sessions", json={
        "developer": "dev", "agent": f"claude-code:{CID[:8]}", "scope": [],
        "description": f"{AUTOREG_DESCRIPTION}; client=claude-code", "auto_lock": False,
        "lifecycle_key": CID})
    assert made.status_code == 201, made.text
    placeholder = made.json()["id"]
    sp.save_pointer(placeholder, CID, write_global=False)
    sp.save_approval_token(placeholder, made.headers["X-ATS-Approval-Token"], cid=CID)
    repo = tmp_path / "repo"
    repo.mkdir()

    child = await _start_as(wired, AGENT_A, "worker A", ["src/a.py"], repo)

    assert child != placeholder, "the subagent claimed the parent's placeholder"
    assert _pointer() == placeholder
    row = await _row(wired, placeholder)
    assert row["status"] == "active" and row["description"].startswith(AUTOREG_DESCRIPTION)


# ── the subagent's own identity ──────────────────────────────────────────────

@pytest.mark.asyncio
async def test_subagent_session_is_linked_to_the_parent_and_has_its_own_pointer(
        wired, tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    parent = await _start_as(wired, None, "coordinator", ["src/parent.py"], repo)
    text = await _call_as(wired, AGENT_A, "start_session", description="worker A",
                          scope=["src/a.py"], repo_root=str(repo))
    child = text.split("Session ID: ")[1].split()[0]

    assert "THIS SUBAGENT's own session" in text and AGENT_A in text, text
    assert sp.read_agent_pointer(CID, AGENT_A) == child
    assert sp.read_agent_pointer(CID, AGENT_B) is None
    ring = sp.capability_entries(CID)
    assert ring[child] == {"state": "open", "parent": parent}
    assert ring[parent]["parent"] is None
    assert f"subagent of session {parent}" in (await _row(wired, child))["description"]


@pytest.mark.asyncio
async def test_each_concurrent_subagent_acts_as_its_own_session_by_default(wired, tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    parent = await _start_as(wired, None, "coordinator", ["src/parent.py"], repo)
    a = await _start_as(wired, AGENT_A, "worker A", ["src/a.py"], repo)
    b = await _start_as(wired, AGENT_B, "worker B", ["src/b.py"], repo)

    for agent, title in ((AGENT_A, "by A"), (AGENT_B, "by B"), (None, "by parent")):
        text = await _call_as(wired, agent, "log_decision", title=title, chosen="x",
                              reasoning="y")
        assert "Decision logged" in text, text
    assert await _decision_titles(wired, a) == ["by A"]
    assert await _decision_titles(wired, b) == ["by B"]
    assert await _decision_titles(wired, parent) == ["by parent"]
    assert _pointer() == parent


@pytest.mark.asyncio
async def test_subagent_completion_never_touches_the_parent(wired, tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    parent = await _start_as(wired, None, "coordinator", ["src/parent.py"], repo)
    child = await _start_as(wired, AGENT_A, "worker A", ["src/a.py"], repo)

    text = await _call_as(wired, AGENT_A, "complete_session", summary="worker done")
    assert "Session completed" in text and child in text, text
    assert (await _row(wired, child))["status"] == "completed"
    assert (await _row(wired, parent))["status"] == "active"
    assert _pointer() == parent

    # The subagent omits session_id again: its identity is still its own
    # (completed) session -- never a silent fallback to the parent's.
    again = await _call_as(wired, AGENT_A, "complete_session", summary="done twice")
    assert "already completed" in again and child in again, again
    assert (await _row(wired, parent))["status"] == "active"
    text = await _call_as(wired, None, "complete_session", summary="coordinator done")
    assert "Session completed" in text and parent in text, text


@pytest.mark.asyncio
async def test_explicit_parent_session_id_from_a_subagent_also_records_its_pointer(
        wired, tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    parent = await _start_as(wired, None, "coordinator", ["src/parent.py"], repo)
    child = await _start_as(wired, AGENT_A, "worker A", ["src/a.py"], repo,
                            parent_session_id=parent)
    assert sp.read_agent_pointer(CID, AGENT_A) == child
    assert _pointer() == parent
    assert sp.capability_entries(CID)[child]["parent"] == parent


# ── what is unchanged ────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_main_thread_second_start_still_becomes_the_default(wired, tmp_path):
    """#3593's contract stands for the main thread: the shared Claude session id
    alone never makes a session a subagent."""
    ra, rb = tmp_path / "repo-a", tmp_path / "repo-b"
    ra.mkdir()
    rb.mkdir()
    a = await _start_as(wired, None, "repository A", ["src/a.py"], ra)
    b = await _start_as(wired, None, "repository B", ["src/b.py"], rb)
    assert _pointer() == b and a != b
    assert sp.capability_entries(CID)[b]["parent"] is None


@pytest.mark.asyncio
async def test_unknown_caller_keeps_the_pre_3677_behaviour(wired, tmp_path):
    """No hook record (hook not installed, or an older client): the caller is
    UNKNOWN, never assumed to be the main thread or a subagent."""
    repo = tmp_path / "repo"
    repo.mkdir()
    await _start_as(wired, None, "coordinator", ["src/parent.py"], repo)
    second = (await _call_as(wired, AGENT_A, "start_session", record=False,
                             description="unrecorded", scope=["src/u.py"],
                             repo_root=str(repo))).split("Session ID: ")[1].split()[0]
    assert _pointer() == second
    assert sp.read_agent_pointer(CID, AGENT_A) is None


# ── the tool-caller record and the agent pointer ─────────────────────────────

def test_tool_caller_record_is_exact_and_unknown_is_never_main_thread(monkeypatch, tmp_path):
    _declare(monkeypatch, tmp_path)
    assert sp.record_tool_caller("toolu_sub", CID, AGENT_A)
    assert sp.record_tool_caller("toolu_main", CID, "")
    assert sp.tool_caller("toolu_sub", CID) == AGENT_A
    assert sp.tool_caller("toolu_main", CID) == ""
    assert sp.tool_caller("toolu_missing", CID) is None
    assert sp.tool_caller("toolu_sub", "another-client-0000") is None
    assert not sp.record_tool_caller("../escape", CID, AGENT_A)
    assert sp.tool_caller("../escape", CID) is None
    d = Path(os.environ["ATS_STATE_DIR"]) / sp.TOOL_CALLER_DIR_NAME
    assert oct(d.stat().st_mode & 0o777) == "0o700"
    assert oct((d / "toolu_sub").stat().st_mode & 0o777) == "0o600"
    old = d / "toolu_sub"
    stale = os.path.getmtime(old) - sp.TOOL_CALLER_TTL_S - 5
    os.utime(old, (stale, stale))
    assert sp.tool_caller("toolu_sub", CID) is None           # expired
    sp.record_tool_caller("toolu_new", CID, AGENT_B)          # sweeps expired ones
    assert not old.exists()


def test_agent_pointer_never_touches_the_client_pointer(monkeypatch, tmp_path):
    _declare(monkeypatch, tmp_path)
    sp.save_pointer("parent-session", CID, write_global=False)
    assert sp.save_agent_pointer("child-session", CID, AGENT_A)
    assert sp.read_session_pointer(CID) == "parent-session"
    assert sp.read_agent_pointer(CID, AGENT_A) == "child-session"
    assert sp.read_agent_pointer(CID, AGENT_B) is None
    assert sp.read_agent_pointer(CID, "") is None
    assert sp.read_agent_pointer("another-client-0000", AGENT_A) is None


def test_hook_records_the_caller_and_never_blocks(monkeypatch, tmp_path):
    _declare(monkeypatch, tmp_path)
    _hook_records("toolu_hooked", AGENT_A)
    assert sp.tool_caller("toolu_hooked", CID) == AGENT_A
    _hook_records("toolu_mainthread", None)
    assert sp.tool_caller("toolu_mainthread", CID) == ""
    for raw in ("not json", "[]", json.dumps({"tool_use_id": "x"})):
        r = subprocess.run([sys.executable, "-m",
                            "ai_team_sync.hooks.pre_tool_use_ats_caller"],
                           input=raw, env=dict(os.environ, PYTHONPATH=str(SOURCE)),
                           capture_output=True, text=True, timeout=60)
        assert r.returncode == 0 and r.stdout == "", (raw, r.stdout, r.stderr)


# ── guard identity: resolve_identity ─────────────────────────────────────────

def test_guard_identity_prefers_the_subagents_own_pointer(monkeypatch, tmp_path):
    _declare(monkeypatch, tmp_path)
    sp.save_pointer("parent-session", CID, write_global=False)
    sp.save_approval_token("child-session", "tok-c", cid=CID)
    sp.save_agent_pointer("child-session", CID, AGENT_A)
    assert cg.resolve_identity(CID, AGENT_A) == cg.Identity("child-session", "agent")
    assert cg.resolve_identity(CID, AGENT_B) == cg.Identity("parent-session", "per_session")
    assert cg.resolve_identity(CID) == cg.Identity("parent-session", "per_session")
    monkeypatch.setenv("ATS_SESSION_ID", "child-session")
    assert cg.resolve_identity(CID, AGENT_A).conflict == ""


def test_an_agent_pointer_naming_a_session_this_client_does_not_hold_is_ignored(
        monkeypatch, tmp_path):
    """Stricter than the client pointer: a stray or planted agent pointer never
    makes another client's session the acting one."""
    _declare(monkeypatch, tmp_path)
    sp.save_pointer("parent-session", CID, write_global=False)
    sp.save_agent_pointer("someone-elses-session", CID, AGENT_A)
    assert cg.resolve_identity(CID, AGENT_A) == cg.Identity("parent-session", "per_session")


def test_guard_identity_env_may_name_an_open_session_this_client_holds(
        monkeypatch, tmp_path):
    """A git hook cannot tell which subagent is committing; a subagent may say
    which of this Claude session's OWN open sessions it is. Anything else is
    still the #3581 conflict."""
    _declare(monkeypatch, tmp_path)
    sp.save_pointer("parent-session", CID, write_global=False)
    sp.save_approval_token("parent-session", "tok-p", cid=CID)
    sp.save_approval_token("child-session", "tok-c", cid=CID)
    sp.save_approval_token("done-session", "tok-d", cid=CID)
    sp.mark_capability_state("done-session", sp.CAP_COMPLETED, cid=CID)

    monkeypatch.setenv("ATS_SESSION_ID", "child-session")
    ident = cg.resolve_identity(CID)
    assert ident == cg.Identity("child-session", "env_held") and ident.self_asserted

    for other in ("someone-else", "done-session"):
        monkeypatch.setenv("ATS_SESSION_ID", other)
        ident = cg.resolve_identity(CID)
        assert ident.session_id == "parent-session"
        assert "contradicts this Claude session's own ATS pointer" in ident.conflict


def test_foreign_holders_never_matches_label_text(monkeypatch):
    """#2893: the cid8-substring fallback is gone; only exact sibling ids."""
    results = [{"path": "f.txt", "matches": [
        {"session_id": "s-forged", "agent": f"x-claude-code:{CID[:8]}-y", "mode": "exclusive",
         "pattern": "f.txt"},
        {"session_id": "s-sibling", "agent": f"claude-code:{CID[:8]}", "mode": "exclusive",
         "pattern": "f.txt"}]}]
    holders = cg.foreign_holders(results, None, {"s-sibling"})
    assert [h.session_id for h in holders] == ["s-forged"]


# ── the guards, end to end (real git, real hooks, real ATS server) ──────────

def _sessions_of_this_claude_session(world, state):
    """Parent P (the pointer) and subagent S, both registered by this Claude
    session (label + capability in its keyring); F carries the same label but
    its capability is NOT held (a forged or foreign row)."""
    def made(description):
        r = httpx.post(f"{world['url']}/api/sessions", json={
            "developer": "pvestal", "agent": f"claude-code:{CID[:8]}", "scope": [],
            "description": description, "repo_root": world["gov_real"]})
        assert r.status_code == 201, r.text
        return r.json()["id"], r.headers["X-ATS-Approval-Token"]

    def lock(sid, pattern, mode="exclusive"):
        r = httpx.post(f"{world['url']}/api/locks", json={
            "session_id": sid, "pattern": pattern, "mode": mode, "reason": "test claim"})
        assert r.status_code == 201, r.text

    (p, tp), (s, ts), (f, _tf) = made("parent"), made("subagent"), made("forged")
    lock(p, "parent.txt")
    lock(s, "sub.txt")
    lock(f, "forged.txt")
    old = os.environ.get("ATS_STATE_DIR")
    os.environ["ATS_STATE_DIR"] = str(state)
    try:
        sp.save_pointer(p, CID, write_global=False)
        sp.save_approval_token(p, tp, cid=CID)
        sp.save_approval_token(s, ts, cid=CID)
        # Written by S's start_session in production; absent on main, where
        # the reproducers below then fail on the guard's verdict itself.
        if hasattr(sp, "save_agent_pointer"):
            sp.save_agent_pointer(s, CID, AGENT_A)
    finally:
        if old is None:
            os.environ.pop("ATS_STATE_DIR", None)
        else:
            os.environ["ATS_STATE_DIR"] = old
    return p, s, f


def _claude_shell(world, **extra):
    """The environment a Claude Bash tool gives a subagent's `git commit`: the
    Claude session id and pid, and nothing that names the subagent."""
    return world["env"](None, CLAUDE_CODE_SESSION_ID=CID, CLAUDE_PID=str(os.getpid()),
                        CLAUDE_CODE_CHILD_SESSION="1", **extra)


def test_reproducer_subagent_commits_its_own_locked_file_while_pointer_names_parent(world):
    state = Path(world["env"]()["ATS_STATE_DIR"])
    p, s, _f = _sessions_of_this_claude_session(world, state)
    r = _commit(world["gov"], _claude_shell(world), path="sub.txt")
    assert r.returncode == 0, r.stderr
    # Visible, not blocking: the path is held by another session of this
    # same Claude session.
    assert "another session of this same Claude session" in r.stderr and s in r.stderr
    own = _commit(world["gov"], _claude_shell(world), path="parent.txt")
    assert own.returncode == 0, own.stderr


def test_a_same_label_session_whose_capability_is_not_held_still_blocks(world):
    state = Path(world["env"]()["ATS_STATE_DIR"])
    _p, _s, f = _sessions_of_this_claude_session(world, state)
    r = _commit(world["gov"], _claude_shell(world), path="forged.txt")
    assert r.returncode != 0 and "REFUSED (pre-commit)" in r.stderr and f in r.stderr
    other = _commit(world["gov"], _claude_shell(world), path="f.txt")   # world's A
    assert other.returncode != 0 and world["a"] in other.stderr


def test_reproducer_ats_session_id_naming_the_subagents_own_session_is_honoured(world):
    state = Path(world["env"]()["ATS_STATE_DIR"])
    _p, s, f = _sessions_of_this_claude_session(world, state)
    r = _commit(world["gov"], _claude_shell(world, ATS_SESSION_ID=s), path="sub.txt")
    assert r.returncode == 0, r.stderr
    assert "contradicts" not in r.stderr
    bad = _commit(world["gov"], _claude_shell(world, ATS_SESSION_ID=f), path="sub.txt",
                  content="again\n")
    assert bad.returncode != 0 and "contradicts" in bad.stderr


def test_reproducer_bash_guard_subagent_writes_its_own_locked_file(world):
    state = Path(world["env"]()["ATS_STATE_DIR"])
    _p, s, f = _sessions_of_this_claude_session(world, state)

    def bash(command, agent):
        payload = {"tool_name": "Bash", "tool_input": {"command": command},
                   "cwd": str(world["gov"]), "session_id": CID, "agent_id": agent}
        return subprocess.run(
            [sys.executable, "-m", "ai_team_sync.hooks.pre_tool_use_bashguard"],
            input=json.dumps(payload), env=_claude_shell(world), capture_output=True,
            text=True, timeout=60)

    ok = bash("echo x > sub.txt", AGENT_A)
    assert ok.returncode == 0, ok.stderr
    sibling = bash("echo x > sub.txt", AGENT_B)      # a subagent without a session
    assert sibling.returncode == 0, sibling.stderr
    forged = bash("echo x > forged.txt", AGENT_A)
    assert forged.returncode == 2 and f in forged.stderr, forged.stderr


def test_edit_guard_resolves_the_subagents_own_session(world):
    state = Path(world["env"]()["ATS_STATE_DIR"])
    _p, s, f = _sessions_of_this_claude_session(world, state)

    def write(path, agent, **env):
        payload = {"tool_name": "Write", "session_id": CID, "agent_id": agent,
                   "tool_input": {"file_path": str(world["gov"] / path)}}
        return subprocess.run(
            [sys.executable, "-m", "ai_team_sync.hooks.pre_tool_use_lockcheck"],
            input=json.dumps(payload), env=_claude_shell(world, **env),
            capture_output=True, text=True, timeout=60)

    assert write("sub.txt", AGENT_A).returncode == 0
    # $ATS_SESSION_ID agreeing with the subagent's own pointer is no conflict...
    assert write("sub.txt", AGENT_A, ATS_SESSION_ID=s).returncode == 0
    # ...one naming a session this Claude session does not hold still is.
    refused = write("sub.txt", AGENT_A, ATS_SESSION_ID=f)
    assert refused.returncode == 2 and "subagent's own ATS pointer" in refused.stderr
    blocked = write("forged.txt", AGENT_A)
    assert blocked.returncode == 2, blocked.stderr
