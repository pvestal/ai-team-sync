"""#3593 operator contract: every session keeps its OWN capability.

One Claude process hosts one stdio MCP server, and every subagent of that
Claude session shares it, its Claude session id and its pointer. Starting a
second ATS session there (a second repository, a second ticket, a subagent)
used to destroy the first session's capability -- in memory (one token slot)
and on disk (one pair written with O_TRUNC, keyed by cid[:8]) -- so the first
session could no longer be completed, paused, or release its own locks, and
was left to the reaper.

Each test below is named for the contract point it pins (ITEM 1: keyring,
ITEM 2: registered subagents). The REPRODUCER tests use only tool arguments
that exist on main (1a3ed91), so the same file shows them failing there.

Identity is declared per test (AGENTS.md "Test validity"); every test runs in
its own temporary ATS_STATE_DIR, and no capability value is printed.
"""
from __future__ import annotations

import asyncio
import io
import json
import multiprocessing
import os
import stat
import sys
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

CID = "7ff8070e-0000-4000-8000-000000000001"


# ── harness ──────────────────────────────────────────────────────────────────

def _envelope(tid):
    from tests.test_task_brief import TASK_ENVELOPE

    return dict(TASK_ENVELOPE, id=int(tid), task_key=f"task-{tid}",
                title=f"Synthetic task {tid}", relations={})


def _fresh_process(monkeypatch, tmp_path, cid=CID):
    """A new MCP process: no in-process sessions, its own client id and state."""
    monkeypatch.setattr(mcp, "_IN_PROCESS_SESSION_ID", None)
    for name in ("_IN_PROCESS_TOKENS", "_IN_PROCESS_STATE", "_IN_PROCESS_PARENT",
                 "_CAPABILITY_PROBLEMS"):
        monkeypatch.setattr(mcp, name, {}, raising=False)
    d = tmp_path / f"state-{uuid.uuid4().hex[:6]}"
    d.mkdir()
    monkeypatch.setenv("ATS_STATE_DIR", str(d))
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", cid)
    monkeypatch.setenv("CLAUDE_PID", str(os.getpid()))
    return d


def _declare_identity(monkeypatch):
    monkeypatch.setenv("ATS_AGENT", "claude-code")
    for var in ("ATS_DELEGATION", "ATS_SESSION_ID", "ATS_SESSION", "ATS_APPROVAL_TOKEN"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setattr(briefs, "fetch_tower_task_data",
                        lambda tid, **kw: (_envelope(tid), None))
    monkeypatch.setattr(briefs, "fetch_task_overlaps",
                        lambda objective, **kw: ([], "Echo stub: not consulted"))
    monkeypatch.setattr(briefs, "recall_memories", lambda *a, **kw: [])


class _Spy:
    """Every request the MCP handlers send: (method, path, params, token)."""

    def __init__(self):
        self.calls = []

    async def __call__(self, request):
        self.calls.append((request.method, request.url.path, dict(request.url.params),
                           request.headers.get("X-ATS-Approval-Token", "")))


@pytest.fixture
def wired(db_engine, monkeypatch, tmp_path):
    app = create_app()
    factory = async_sessionmaker(db_engine, class_=AsyncSession, expire_on_commit=False)

    async def override_get_db():
        async with factory() as s:
            yield s

    app.dependency_overrides[get_db] = override_get_db
    transport = ASGITransport(app=app)
    spy = _Spy()

    class _ASGIClient:
        def __init__(self, *a, **k):
            self._c = AsyncClient(transport=transport, base_url="http://localhost:8400",
                                  event_hooks={"request": [spy]})

        async def __aenter__(self):
            return self._c

        async def __aexit__(self, *exc):
            await self._c.aclose()

    monkeypatch.setattr(mcp.httpx, "AsyncClient", _ASGIClient)
    _declare_identity(monkeypatch)
    _fresh_process(monkeypatch, tmp_path)
    transport.spy = spy
    return transport


async def _rest(transport, method, path, **kw):
    async with AsyncClient(transport=transport, base_url="http://localhost:8400") as c:
        return await getattr(c, method)(path, **kw)


async def _row(transport, sid):
    return (await _rest(transport, "get", f"/api/sessions/{sid}")).json()


async def _locks_of(transport, sid):
    return [lk for lk in (await _rest(transport, "get", "/api/locks")).json()
            if lk["session_id"] == sid]


async def _call(tool, **args):
    return (await mcp.call_tool(tool, args))[0].text


async def _start(ticket, repo, description, **extra):
    text = await _call("start_session", description=description,
                       scope=[f"src/t{ticket}.py"], repo_root=str(repo),
                       ticket_id=ticket, **extra)
    assert "Session started" in text, text
    return text.split("Session ID: ")[1].split()[0]


async def _a_then_b(tmp_path):
    """start A (repository 1, ticket 3591), then start B (repository 2)."""
    ra, rb = tmp_path / "ai-team-sync", tmp_path / "tower-echo-brain"
    ra.mkdir(exist_ok=True)
    rb.mkdir(exist_ok=True)
    a = await _start(3591, ra, "#3591 owner, ATS repository")
    b = await _start(4242, rb, "Echo-side work for the same change")
    return a, b


def _token_in_process(sid):
    return mcp._IN_PROCESS_TOKENS.get(sid, "")


def _keyring(cid=CID):
    path = sp.capability_store_path(cid)
    return json.loads(path.read_text())["sessions"] if path.exists() else {}


# ── ITEM 1 / REPRODUCER: start A -> start B -> operate on A ──────────────────
#
# Only arguments that exist on main: complete_session's session_id, and the
# lock / delegation ids. pause_session and log_decision did not take a
# session_id on main; there the argument is ignored and the call acts on B,
# which the assertions on A's row catch.

async def _op_complete(transport, a, b):
    text = await _call("complete_session", session_id=a, summary="done with A")
    assert "Session completed" in text, text
    assert (await _row(transport, a))["status"] == "completed"


async def _op_pause(transport, a, b):
    text = await _call("pause_session", session_id=a)
    assert "paused" in text and a in text, text
    assert (await _row(transport, a))["status"] == "paused"


async def _op_log_decision(transport, a, b):
    text = await _call("log_decision", session_id=a, title="decided as A",
                       chosen="x", reasoning="y")
    assert "Decision logged" in text, text
    titles = [d["title"] for d in (await _rest(
        transport, "get", "/api/decisions", params={"session_id": a})).json()]
    assert "decided as A" in titles, titles


async def _op_delete_lock(transport, a, b):
    (lock,) = await _locks_of(transport, a)
    text = await _call("delete_lock", lock_id=lock["id"])
    assert "Lock deleted" in text, text
    assert await _locks_of(transport, a) == []


async def _op_reconcile(transport, a, b):
    made = await _rest(transport, "post", "/api/delegations", json={
        "parent_session_id": a, "delegated_worker": "codex", "mode": "READ_ONLY",
        "acceptance": "a verdict"})
    assert made.status_code == 201, made.text
    text = await _call("reconcile_delegation", delegation_id=made.json()["id"],
                       state="closed", verdict="checked the child's claims myself")
    assert "CLOSED" in text and a in text, text


_OPS = {"complete": _op_complete, "pause": _op_pause, "log_decision": _op_log_decision,
        "delete_lock": _op_delete_lock, "reconcile": _op_reconcile}


@pytest.mark.asyncio
@pytest.mark.parametrize("op", list(_OPS))
async def test_reproducer_start_a_start_b_then_operate_on_a(wired, tmp_path, op):
    a, b = await _a_then_b(tmp_path)
    await _OPS[op](wired, a, b)
    # B was never touched by an operation on A.
    row_b = await _row(wired, b)
    assert row_b["status"] == "active", row_b
    assert len(await _locks_of(wired, b)) == 1


@pytest.mark.asyncio
async def test_reproducer_every_operation_on_a_in_one_sequence(wired, tmp_path):
    a, b = await _a_then_b(tmp_path)
    for op in ("log_decision", "reconcile", "delete_lock", "pause", "complete"):
        await _OPS[op](wired, a, b)
    assert (await _row(wired, b))["status"] == "active"


# ── ITEM 1: the stores ────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_item1_starting_b_keeps_a_in_the_file_store_and_in_process(wired, tmp_path):
    a, b = await _a_then_b(tmp_path)
    ring = _keyring()
    assert set(ring) == {a, b}
    assert {s: e["state"] for s, e in ring.items()} == {a: "open", b: "open"}
    assert ring[a]["token"] == _token_in_process(a) != ""
    assert ring[b]["token"] == _token_in_process(b) != ""
    assert ring[a]["token"] != ring[b]["token"]
    # Both still authorize their own row on the server.
    for sid in (a, b):
        r = await _rest(wired, "get", f"/api/sessions/{sid}/messages",
                        headers={"X-ATS-Approval-Token": ring[sid]["token"]})
        assert r.status_code == 200, (sid, r.text)


@pytest.mark.asyncio
async def test_item1_an_operation_naming_a_presents_a_capability_never_the_pointers(
        wired, tmp_path):
    a, b = await _a_then_b(tmp_path)
    assert sp.resolve_pointer() == b                 # the pointer names B
    wired.spy.calls.clear()
    await _op_pause(wired, a, b)
    patches = [c for c in wired.spy.calls if c[0] == "PATCH"]
    assert [(p[1], p[3] == _token_in_process(a)) for p in patches] == [
        (f"/api/sessions/{a}", True)]
    assert all(c[3] != _token_in_process(b) for c in wired.spy.calls if a in c[1])


@pytest.mark.asyncio
async def test_item1_after_an_mcp_restart_a_is_loaded_from_the_keyring(
        wired, tmp_path, monkeypatch):
    """The in-memory copy is gone; the keyring holds A, and naming A uses it."""
    a, b = await _a_then_b(tmp_path)
    for name in ("_IN_PROCESS_TOKENS", "_IN_PROCESS_STATE", "_IN_PROCESS_PARENT"):
        monkeypatch.setattr(mcp, name, {})
    monkeypatch.setattr(mcp, "_IN_PROCESS_SESSION_ID", None)
    await _op_pause(wired, a, b)
    await _op_complete(wired, a, b)
    assert (await _row(wired, b))["status"] == "active"
    assert _keyring()[a]["state"] == "completed"


@pytest.mark.asyncio
async def test_item1_the_pointer_is_only_the_default_selector(wired, tmp_path):
    a, b = await _a_then_b(tmp_path)
    assert sp.resolve_pointer() == b and mcp._IN_PROCESS_SESSION_ID == b
    text = await _call("pause_session")              # no session named: the default
    assert b in text, text
    assert (await _row(wired, b))["status"] == "paused"
    assert (await _row(wired, a))["status"] == "active"
    assert sp.resolve_pointer() == b                 # naming A never moved it
    await _call("pause_session", session_id=a)
    assert sp.resolve_pointer() == b


@pytest.mark.asyncio
async def test_item1_a_missing_capability_fails_explicitly_and_never_falls_back(
        wired, tmp_path, monkeypatch):
    a, b = await _a_then_b(tmp_path)
    # A's capability is gone from memory AND from the keyring; B's is held.
    mcp._IN_PROCESS_TOKENS.pop(a)
    path = sp.capability_store_path(CID)
    data = json.loads(path.read_text())
    data["sessions"].pop(a)
    path.write_text(json.dumps(data))
    wired.spy.calls.clear()
    for tool, args in (("complete_session", {"session_id": a, "summary": "x"}),
                       ("pause_session", {"session_id": a}),
                       ("log_decision", {"session_id": a, "title": "t", "chosen": "c",
                                         "reasoning": "r"})):
        text = await _call(tool, **args)
        # Refused, naming A. complete_session refuses one step earlier: with
        # A's capability gone, B (the live pointer) is the only session this
        # process can prove is its own, so naming A is a pointer conflict.
        assert text.startswith("❌") and a in text, (tool, text)
        assert "capability" in text or "DIFFERENT live" in text, (tool, text)
    assert not [c for c in wired.spy.calls if c[0] in ("PATCH", "POST") and a in c[1]]
    assert not [c for c in wired.spy.calls if c[0] == "POST" and c[1] == "/api/decisions"]
    assert not [c for c in wired.spy.calls if c[3] == _token_in_process(b) and a in c[1]]
    assert (await _row(wired, a))["status"] == "active"


@pytest.mark.asyncio
async def test_item1_a_malformed_keyring_is_refused_never_read_as_empty_or_overwritten(
        wired, tmp_path):
    a, b = await _a_then_b(tmp_path)
    mcp._IN_PROCESS_TOKENS.pop(a)
    path = sp.capability_store_path(CID)
    path.write_text('{"version": 2, "client_id": "' + CID + '", "sessions": {BROKEN')
    before = path.read_bytes()
    with pytest.raises(sp.CapabilityStoreError):
        sp.load_approval_token(a, cid=CID)
    with pytest.raises(sp.CapabilityStoreError):
        sp.save_approval_token("sess-new", "tok-new", cid=CID)
    assert path.read_bytes() == before
    text = await _call("pause_session", session_id=a)
    assert text.startswith("❌") and "keyring" in text, text
    # A new start still works, says plainly that it was not persisted, and the
    # malformed store is left exactly as it was.
    repo = tmp_path / "third"
    repo.mkdir()
    text = await _call("start_session", description="third", scope=["x.py"],
                       repo_root=str(repo), ticket_id=5151)
    assert "Session started" in text and "NOT saved" in text, text
    assert path.read_bytes() == before


@pytest.mark.parametrize("bad", [
    "", "not json", "[]", '{"version": 1, "sessions": {}}',
    '{"version": 2, "client_id": "someone-else", "sessions": {}}',
    '{"version": 2, "client_id": "%s", "sessions": []}' % CID,
    '{"version": 2, "client_id": "%s", "sessions": {"s": {"token": ""}}}' % CID,
    '{"version": 2, "client_id": "%s", "sessions": {"s": {"token": "t", "state": "x"}}}' % CID,
])
def test_item1_every_malformed_keyring_shape_is_refused(tmp_path, monkeypatch, bad):
    monkeypatch.setenv("ATS_STATE_DIR", str(tmp_path))
    sp.save_approval_token("seed", "tok-seed", cid=CID)
    path = sp.capability_store_path(CID)
    path.write_text(bad)
    with pytest.raises(sp.CapabilityStoreError):
        sp.save_approval_token("other", "tok-other", cid=CID)
    with pytest.raises(sp.CapabilityStoreError):
        sp.load_approval_token("seed", cid=CID)
    assert path.read_text() == bad


@pytest.mark.asyncio
async def test_item1_a_revoked_capability_is_refused_and_never_presented_again(
        wired, tmp_path):
    a, b = await _a_then_b(tmp_path)
    mcp._IN_PROCESS_TOKENS[a] = "not-the-capability"   # the server will refuse it
    text = await _call("pause_session", session_id=a)
    assert "403" in text or text.startswith("❌"), text
    assert mcp._IN_PROCESS_STATE.get(a) == "revoked"
    assert _keyring()[a]["state"] == "revoked"
    wired.spy.calls.clear()
    text = await _call("pause_session", session_id=a)
    assert text.startswith("❌") and "refused" in text, text
    assert not [c for c in wired.spy.calls if c[0] == "PATCH"]
    assert sp.load_approval_token(a, cid=CID) == ""
    assert (await _row(wired, a))["status"] == "active"
    assert (await _row(wired, b))["status"] == "active"


@pytest.mark.asyncio
async def test_item1_a_wrong_sessions_capability_is_refused_by_the_server(wired, tmp_path):
    """Server-side checks are unchanged: B's capability never moves A."""
    a, b = await _a_then_b(tmp_path)
    r = await _rest(wired, "patch", f"/api/sessions/{a}", json={"status": "paused"},
                    headers={"X-ATS-Approval-Token": _token_in_process(b)})
    assert r.status_code == 403, r.text
    assert (await _row(wired, a))["status"] == "active"


@pytest.mark.asyncio
async def test_item1_a_completed_session_is_not_revived(wired, tmp_path):
    a, b = await _a_then_b(tmp_path)
    await _op_complete(wired, a, b)
    assert _keyring()[a]["state"] == "completed"
    text = await _call("resume_session", session_id=a)
    assert (await _row(wired, a))["status"] == "completed", text
    # Bookkeeping is monotone: nothing turns a completed entry open again.
    assert sp.mark_capability_state(a, sp.CAP_COMPLETED, cid=CID) is False
    assert _keyring()[a]["state"] == "completed"
    # ...but its capability is kept (a completed sender may still readdress).
    assert sp.load_approval_token(a, cid=CID) == _token_in_process(a) != ""


@pytest.mark.asyncio
async def test_item1_no_arbitrary_lock_release_and_no_privilege_expansion(wired, tmp_path):
    """Holding A and B gives exactly A's and B's authority: never C's."""
    r = await _rest(wired, "post", "/api/sessions", json={
        "developer": "dev", "agent": "claude-code:7ff8070e", "scope": ["src/c.py"],
        "description": "another session with the SAME label", "auto_lock": True})
    c = r.json()["id"]
    (c_lock,) = await _locks_of(wired, c)
    made = await _rest(wired, "post", "/api/delegations", json={
        "parent_session_id": c, "delegated_worker": "codex", "mode": "READ_ONLY",
        "acceptance": "a verdict"})
    deleg = made.json()["id"]
    a, b = await _a_then_b(tmp_path)

    text = await _call("delete_lock", lock_id=c_lock["id"])
    assert "Lock deleted" not in text, text
    assert len(await _locks_of(wired, c)) == 1
    for sid in (a, c):
        text = await _call("delete_lock", lock_id=c_lock["id"], session_id=sid)
        assert "Lock deleted" not in text, (sid, text)
    assert len(await _locks_of(wired, c)) == 1
    text = await _call("reconcile_delegation", delegation_id=deleg, state="closed",
                       verdict="v")
    assert "CLOSED" not in text, text
    for tool, args in (("complete_session", {"session_id": c, "summary": "x"}),
                       ("pause_session", {"session_id": c}),
                       ("extend_scope", {"session_id": c, "patterns": ["docs/**"]}),
                       ("log_decision", {"session_id": c, "title": "t", "chosen": "c",
                                         "reasoning": "r"})):
        text = await _call(tool, **args)
        assert text.startswith("❌"), (tool, text)
    assert (await _row(wired, c))["status"] == "active"


# ── ITEM 1: atomic writes, permissions, concurrency ──────────────────────────

def test_item1_the_keyring_dir_is_0700_and_files_0600_even_if_loosened(tmp_path, monkeypatch):
    monkeypatch.setenv("ATS_STATE_DIR", str(tmp_path))
    d = tmp_path / sp.CAPABILITY_DIR_NAME
    d.mkdir(mode=0o755)
    os.chmod(d, 0o755)
    sp.save_approval_token("a", "tok-a", cid=CID)
    assert stat.S_IMODE(d.stat().st_mode) == 0o700
    assert stat.S_IMODE(sp.capability_store_path(CID).stat().st_mode) == 0o600
    assert stat.S_IMODE(sp.capability_store_path(CID).with_suffix(".lock").stat().st_mode) == 0o600


def test_item1_a_symlinked_keyring_dir_is_refused(tmp_path, monkeypatch):
    monkeypatch.setenv("ATS_STATE_DIR", str(tmp_path))
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    (tmp_path / sp.CAPABILITY_DIR_NAME).symlink_to(elsewhere)
    with pytest.raises(sp.CapabilityStoreError):
        sp.save_approval_token("a", "tok-a", cid=CID)
    assert list(elsewhere.iterdir()) == []


# r1 review (Codex BLOCK, contract point 5): reads and the lock file are held to
# the same rules as the directory -- no symlink, this user's, private.

def _held(tmp_path, monkeypatch):
    monkeypatch.setenv("ATS_STATE_DIR", str(tmp_path))
    sp.save_approval_token("a", "tok-a", cid=CID)
    return sp.capability_store_path(CID)


@pytest.mark.parametrize("read", ["load", "entries"])
def test_r1_a_load_through_a_symlinked_keyring_dir_is_refused(tmp_path, monkeypatch, read):
    store = _held(tmp_path, monkeypatch)
    d = store.parent
    moved = tmp_path / "moved"
    d.rename(moved)
    d.symlink_to(moved)
    with pytest.raises(sp.CapabilityStoreError):
        sp.load_approval_token("a", cid=CID) if read == "load" else sp.capability_entries(CID)


@pytest.mark.parametrize("op", ["load", "entries", "save", "mark"])
def test_r1_a_symlinked_store_file_is_refused_on_every_path(tmp_path, monkeypatch, op):
    store = _held(tmp_path, monkeypatch)
    real = tmp_path / "planted.json"
    real.write_bytes(store.read_bytes())
    os.chmod(real, 0o600)
    store.unlink()
    store.symlink_to(real)
    before = real.read_bytes()
    with pytest.raises(sp.CapabilityStoreError):
        {"load": lambda: sp.load_approval_token("a", cid=CID),
         "entries": lambda: sp.capability_entries(CID),
         "save": lambda: sp.save_approval_token("b", "tok-b", cid=CID),
         "mark": lambda: sp.mark_capability_state("a", sp.CAP_COMPLETED, cid=CID)}[op]()
    assert real.read_bytes() == before and store.is_symlink()


def test_r1_a_symlinked_lock_file_is_refused_and_its_target_untouched(tmp_path, monkeypatch):
    store = _held(tmp_path, monkeypatch)
    lock = store.with_suffix(".lock")
    target = tmp_path / "victim"
    target.write_text("keep")
    os.chmod(target, 0o644)
    lock.unlink()
    lock.symlink_to(target)
    before = store.read_bytes()
    with pytest.raises(sp.CapabilityStoreError):
        sp.save_approval_token("b", "tok-b", cid=CID)
    assert store.read_bytes() == before
    assert target.read_text() == "keep" and stat.S_IMODE(target.stat().st_mode) == 0o644


@pytest.mark.parametrize("op", ["load", "save"])
def test_r1_a_foreign_owned_store_or_lock_is_refused(tmp_path, monkeypatch, op):
    store = _held(tmp_path, monkeypatch)
    me = os.getuid()
    real_fstat = os.fstat

    class _St:
        def __init__(self, st):
            self._st = st

        def __getattr__(self, k):
            return me + 1 if k == "st_uid" else getattr(self._st, k)

    # the directory stays ours (lstat is untouched); every opened FILE reports a
    # foreign owner, as a store/lock planted by another user would
    monkeypatch.setattr(sp.os, "fstat", lambda fd: _St(real_fstat(fd)))
    before = store.read_bytes()
    with pytest.raises(sp.CapabilityStoreError, match="owned by uid"):
        sp.load_approval_token("a", cid=CID) if op == "load" else \
            sp.save_approval_token("b", "tok-b", cid=CID)
    assert store.read_bytes() == before


@pytest.mark.parametrize("read", ["load", "entries", "save"])
def test_r1_a_store_readable_by_others_is_refused_not_used(tmp_path, monkeypatch, read):
    store = _held(tmp_path, monkeypatch)
    os.chmod(store, 0o644)
    before = store.read_bytes()
    with pytest.raises(sp.CapabilityStoreError, match="readable by others"):
        {"load": lambda: sp.load_approval_token("a", cid=CID),
         "entries": lambda: sp.capability_entries(CID),
         "save": lambda: sp.save_approval_token("b", "tok-b", cid=CID)}[read]()
    assert store.read_bytes() == before


def test_r1_a_loose_existing_lock_file_is_tightened_and_the_save_proceeds(tmp_path, monkeypatch):
    store = _held(tmp_path, monkeypatch)
    lock = store.with_suffix(".lock")
    os.chmod(lock, 0o666)
    sp.save_approval_token("b", "tok-b", cid=CID)
    assert stat.S_IMODE(lock.stat().st_mode) == 0o600
    assert sp.load_approval_token("b", cid=CID) == "tok-b"


def test_r1_a_store_under_a_foreign_owned_dir_is_refused_on_read(tmp_path, monkeypatch):
    store = _held(tmp_path, monkeypatch)
    real_lstat = os.lstat
    d = str(store.parent)

    class _St:
        def __init__(self, st):
            self._st = st

        def __getattr__(self, k):
            return os.getuid() + 1 if k == "st_uid" else getattr(self._st, k)

    monkeypatch.setattr(sp.os, "lstat",
                        lambda p, *a, **k: _St(real_lstat(p)) if str(p) == d else real_lstat(p))
    with pytest.raises(sp.CapabilityStoreError, match="owned by uid"):
        sp.load_approval_token("a", cid=CID)


def test_item1_a_failed_atomic_write_leaves_the_store_intact(tmp_path, monkeypatch):
    monkeypatch.setenv("ATS_STATE_DIR", str(tmp_path))
    sp.save_approval_token("a", "tok-a", cid=CID)
    path = sp.capability_store_path(CID)
    before = path.read_bytes()

    def boom(*_a, **_k):
        raise OSError("disk full")

    monkeypatch.setattr(sp.os, "replace", boom)
    with pytest.raises(sp.CapabilityStoreError):
        sp.save_approval_token("b", "tok-b", cid=CID)
    monkeypatch.undo()
    assert path.read_bytes() == before
    assert [p.name for p in path.parent.iterdir() if p.name.endswith(".tmp")] == []
    monkeypatch.setenv("ATS_STATE_DIR", str(tmp_path))
    assert sp.load_approval_token("a", cid=CID) == "tok-a"
    assert sp.load_approval_token("b", cid=CID) == ""


def test_item1_a_failed_flock_never_proceeds_unlocked(tmp_path, monkeypatch):
    import fcntl

    monkeypatch.setenv("ATS_STATE_DIR", str(tmp_path))
    sp.save_approval_token("a", "tok-a", cid=CID)
    before = sp.capability_store_path(CID).read_bytes()

    def broken(*_a, **_k):
        raise OSError("flock not supported here")

    monkeypatch.setattr(fcntl, "flock", broken)
    with pytest.raises(sp.CapabilityStoreError):
        sp.save_approval_token("b", "tok-b", cid=CID)
    with pytest.raises(sp.CapabilityStoreError):
        sp.mark_capability_state("a", sp.CAP_COMPLETED, cid=CID)
    assert sp.capability_store_path(CID).read_bytes() == before


def test_item1_a_held_lock_times_out_instead_of_writing_unlocked(tmp_path, monkeypatch):
    import fcntl

    monkeypatch.setenv("ATS_STATE_DIR", str(tmp_path))
    sp.save_approval_token("a", "tok-a", cid=CID)
    before = sp.capability_store_path(CID).read_bytes()
    holder = os.open(sp.capability_store_path(CID).with_suffix(".lock"), os.O_RDWR)
    try:
        fcntl.flock(holder, fcntl.LOCK_EX)
        monkeypatch.setattr(sp, "_LOCK_TIMEOUT_S", 0.2)
        with pytest.raises(sp.CapabilityStoreError, match="timed out"):
            sp.save_approval_token("b", "tok-b", cid=CID)
    finally:
        os.close(holder)
    assert sp.capability_store_path(CID).read_bytes() == before


def _concurrent_saver(state, worker, count):
    os.environ["ATS_STATE_DIR"] = state
    from ai_team_sync import session_pointer as p

    for i in range(count):
        p.save_approval_token(f"w{worker}-s{i}", f"tok-{worker}-{i}", cid=CID)


def test_item1_concurrent_keyring_updates_keep_every_entry(tmp_path, monkeypatch):
    """Eight processes (a parent and its subagents' hooks) write one keyring at
    once; every open entry survives, past APPROVAL_TOKEN_MAX too."""
    monkeypatch.setenv("ATS_STATE_DIR", str(tmp_path))
    ctx = multiprocessing.get_context("spawn")
    procs = [ctx.Process(target=_concurrent_saver, args=(str(tmp_path), w, 6))
             for w in range(8)]
    for p in procs:
        p.start()
    for p in procs:
        p.join(60)
        assert p.exitcode == 0
    ring = _keyring()
    assert len(ring) == 48 > sp.APPROVAL_TOKEN_MAX
    for w in range(8):
        for i in range(6):
            assert sp.load_approval_token(f"w{w}-s{i}", cid=CID) == f"tok-{w}-{i}"


# ── ITEM 1: eviction (review r2 blocker 1) ───────────────────────────────────

def test_item1_eviction_never_drops_an_open_session(tmp_path, monkeypatch):
    monkeypatch.setenv("ATS_STATE_DIR", str(tmp_path))
    n = sp.APPROVAL_TOKEN_MAX + 8
    for i in range(n):
        sp.save_approval_token(f"s{i}", f"tok-{i}", cid=CID)
    assert len(_keyring()) == n
    assert all(sp.load_approval_token(f"s{i}", cid=CID) == f"tok-{i}" for i in range(n))


def test_item1_eviction_drops_the_oldest_non_open_entries_first(tmp_path, monkeypatch):
    monkeypatch.setenv("ATS_STATE_DIR", str(tmp_path))
    for i in range(sp.APPROVAL_TOKEN_MAX):
        sp.save_approval_token(f"s{i}", f"tok-{i}", cid=CID)
    for i in (5, 9):
        sp.mark_capability_state(f"s{i}", sp.CAP_COMPLETED, cid=CID)
    sp.save_approval_token("new-1", "tok-new-1", cid=CID)
    ring = _keyring()
    assert "s5" not in ring and "s9" in ring and len(ring) == sp.APPROVAL_TOKEN_MAX
    sp.save_approval_token("new-2", "tok-new-2", cid=CID)
    sp.save_approval_token("new-3", "tok-new-3", cid=CID)  # nothing evictable: grows
    ring = _keyring()
    assert "s9" not in ring and len(ring) == sp.APPROVAL_TOKEN_MAX + 1
    assert all(e["state"] == "open" for e in ring.values())


def test_item1_in_process_eviction_never_drops_an_open_session(monkeypatch):
    for name in ("_IN_PROCESS_TOKENS", "_IN_PROCESS_STATE", "_IN_PROCESS_PARENT",
                 "_CAPABILITY_PROBLEMS"):
        monkeypatch.setattr(mcp, name, {})
    monkeypatch.setattr(mcp, "_IN_PROCESS_SESSION_ID", None)
    n = sp.APPROVAL_TOKEN_MAX + 5
    for i in range(n):
        mcp._remember_in_process(f"s{i}", f"tok-{i}")
    assert len(mcp._IN_PROCESS_TOKENS) == n
    mcp._retire_in_process("s3")
    mcp._remember_in_process("late", "tok-late")
    assert "s3" not in mcp._IN_PROCESS_TOKENS and len(mcp._IN_PROCESS_TOKENS) == n
    assert all(f"s{i}" in mcp._IN_PROCESS_TOKENS for i in range(n) if i != 3)


# ── ITEM 1: full client id keys + legacy migration (review r2 blocker 2) ─────

TWIN = CID[:8] + "-ffff-4fff-8fff-ffffffffffff"   # same eight characters


def test_item1_two_claude_sessions_sharing_eight_characters_never_share_state(
        tmp_path, monkeypatch):
    monkeypatch.setenv("ATS_STATE_DIR", str(tmp_path))
    sp.save_approval_token("mine", "tok-mine", cid=CID)
    sp.save_pointer("mine", cid=CID)
    sp.save_approval_token("theirs", "tok-theirs", cid=TWIN)
    sp.save_pointer("theirs", cid=TWIN)
    assert sp.load_approval_token("theirs", cid=CID) == ""
    assert sp.load_approval_token("mine", cid=TWIN) == ""
    assert sp.resolve_pointer(cid=CID, allow_global=False) == "mine"
    assert sp.resolve_pointer(cid=TWIN, allow_global=False) == "theirs"
    assert sp.session_pointer_path(CID) != sp.session_pointer_path(TWIN)
    assert not (tmp_path / f"{sp.APPROVAL_TOKEN_PREFIX}{CID[:8]}").exists()
    assert not (tmp_path / f"{sp.GLOBAL_FILE_NAME}_{CID[:8]}").exists()


def test_item1_an_odd_client_id_never_names_a_path_outside_the_state_dir(tmp_path, monkeypatch):
    monkeypatch.setenv("ATS_STATE_DIR", str(tmp_path))
    odd = "../../etc/x"
    sp.save_approval_token("s", "tok", cid=odd)
    path = sp.capability_store_path(odd)
    assert path.parent == tmp_path / sp.CAPABILITY_DIR_NAME and "/" not in path.name
    assert sp.load_approval_token("s", cid=odd) == "tok"


def test_item1_legacy_migration_when_the_file_itself_names_the_session(tmp_path, monkeypatch):
    monkeypatch.setenv("ATS_STATE_DIR", str(tmp_path))
    legacy = tmp_path / f"{sp.APPROVAL_TOKEN_PREFIX}{CID[:8]}"
    legacy.write_text(json.dumps({"session_id": "old-a", "token": "tok-old-a"}))
    (tmp_path / f"{sp.GLOBAL_FILE_NAME}_{CID[:8]}").write_text("old-a")
    assert sp.resolve_pointer(cid=CID, allow_global=False) == "old-a"
    assert sp.load_approval_token("old-a", cid=CID) == "tok-old-a"
    assert sp.held_capability_sessions(CID) == {"old-a"}
    # The never-deployed first-#3593 map shape is verifiable the same way.
    legacy.write_text(json.dumps({"sessions": {"m1": "tok-m1", "m2": "tok-m2"}}))
    assert sp.load_approval_token("m2", cid=CID) == "tok-m2"


@pytest.mark.parametrize("content", [
    {"token": "tok-orphan"},                                      # names no session
    {"session_id": "someone-else", "token": "tok-else"},          # names another one
    {"session_id": "old-a", "token": "tok", "client_id": TWIN},   # another client's
    "garbage",
])
def test_item1_legacy_migration_refused_when_unverifiable(tmp_path, monkeypatch, content):
    """The pointer naming 'old-a' is never evidence that a token is old-a's."""
    monkeypatch.setenv("ATS_STATE_DIR", str(tmp_path))
    legacy = tmp_path / f"{sp.APPROVAL_TOKEN_PREFIX}{CID[:8]}"
    legacy.write_text(content if isinstance(content, str) else json.dumps(content))
    (tmp_path / f"{sp.GLOBAL_FILE_NAME}_{CID[:8]}").write_text("old-a")
    assert sp.resolve_pointer(cid=CID, allow_global=False) == "old-a"
    assert sp.load_approval_token("old-a", cid=CID) == ""
    assert "old-a" not in sp.held_capability_sessions(CID)


def test_item1_clearing_the_pointer_never_resurrects_a_legacy_one(tmp_path, monkeypatch):
    monkeypatch.setenv("ATS_STATE_DIR", str(tmp_path))
    (tmp_path / f"{sp.GLOBAL_FILE_NAME}_{CID[:8]}").write_text("stale-legacy")
    sp.save_pointer("current", cid=CID)
    assert sp.resolve_pointer(cid=CID, allow_global=False) == "current"
    sp.clear_pointer(cid=CID)
    assert sp.resolve_pointer(cid=CID, allow_global=False) is None
    assert sp.resolve_pointer_source(cid=CID)[1] != "per_session"


# ── ITEM 1: the CLI ──────────────────────────────────────────────────────────

def _cli_env(monkeypatch, tmp_path, url, cid):
    state = tmp_path / f"cli-{uuid.uuid4().hex[:6]}"
    state.mkdir()
    env = {"ATS_SERVER_URL": url, "ATS_STATE_DIR": str(state), "ATS_AGENT": "claude-code",
           "CLAUDE_CODE_SESSION_ID": cid, "ATS_DEVELOPER": "dev"}
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    for var in ("ATS_SESSION_ID", "ATS_SESSION", "ATS_DELEGATION", "CLAUDE_PID"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.chdir(tmp_path)
    return state


def test_item1_cli_start_a_start_b_then_pause_and_complete_a(live_ats, tmp_path, monkeypatch):
    from click.testing import CliRunner

    from ai_team_sync.cli import cli

    url, client, _env = live_ats
    _cli_env(monkeypatch, tmp_path, url, "c1100000-0000-4000-8000-000000000001")
    runner = CliRunner()
    ids = []
    for pat in ("docs/a/**", "docs/b/**"):
        out = runner.invoke(cli, ["session", "start", "-s", pat, "-d", pat])
        assert out.exit_code == 0, out.output
        ids.append(out.output.split("Session started: ")[1].split()[0])
    a, b = ids
    out = runner.invoke(cli, ["session", "pause", "--session-id", a])
    assert out.exit_code == 0, out.output
    assert client.get(f"/api/sessions/{a}").json()["status"] == "paused"
    out = runner.invoke(cli, ["session", "complete", "--session-id", a, "-m", "done A"])
    assert out.exit_code == 0, out.output
    assert client.get(f"/api/sessions/{a}").json()["status"] == "completed"
    assert client.get(f"/api/sessions/{b}").json()["status"] == "active"
    assert sp.resolve_pointer(allow_global=False) == b       # B's pointer untouched
    out = runner.invoke(cli, ["session", "complete", "-m", "done B"])
    assert out.exit_code == 0 and b in out.output, out.output


def test_item1_cli_refuses_a_session_whose_capability_it_does_not_hold(
        live_ats, tmp_path, monkeypatch):
    from click.testing import CliRunner

    from ai_team_sync.cli import cli

    url, client, _env = live_ats
    _cli_env(monkeypatch, tmp_path, url, "c1100000-0000-4000-8000-000000000002")
    foreign = client.post("/api/sessions", json={
        "developer": "dev", "agent": "claude-code:c1100000", "scope": [],
        "description": "not this shell's"}).json()["id"]
    runner = CliRunner()
    out = runner.invoke(cli, ["session", "pause", "--session-id", foreign])
    assert out.exit_code == 1 and "No capability" in out.output, out.output
    assert client.get(f"/api/sessions/{foreign}").json()["status"] == "active"


# ── ITEM 1: the hooks ────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_item1_sessionstart_registration_keeps_every_earlier_capability(
        wired, tmp_path):
    """A SessionStart re-registration for this same client (pointer cleared,
    e.g. after the default session completed) adds its placeholder's capability
    beside A's and B's; it used to O_TRUNC the file down to the placeholder."""
    from ai_team_sync.hooks import session_registration as reg

    a, b = await _a_then_b(tmp_path)
    sp.clear_pointer()
    async with AsyncClient(transport=wired, base_url="http://localhost:8400") as c:
        placeholder = await reg.ensure_session("http://localhost:8400", c, reg.RegistrationInput(
            lifecycle_session_id=CID, agent="claude-code", cwd=str(tmp_path)))
    assert placeholder and placeholder not in (a, b)
    for sid in (a, b, placeholder):
        assert sp.load_approval_token(sid, cid=CID), sid
    await _op_complete(wired, a, b)


@pytest.mark.asyncio
async def test_item1_prompt_and_presence_hooks_read_the_exact_sessions_capability(
        wired, tmp_path):
    from ai_team_sync.hooks import ats_context

    a, b = await _a_then_b(tmp_path)
    assert ats_context._token(a, CID) == _token_in_process(a)
    assert ats_context._token(b, CID) == _token_in_process(b)
    assert ats_context._token("never-opened", CID) == ""
    sp.capability_store_path(CID).write_text("{broken")
    assert ats_context._token(a, CID) == ""        # refused, reported, never guessed


# ── ITEM 2: registered subagents ─────────────────────────────────────────────

async def _parent(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir(exist_ok=True)
    return repo, await _start(3591, repo, "parent: owns #3591")


@pytest.mark.asyncio
async def test_item2_a_subagent_gets_its_own_identity_without_replacing_the_parent(
        wired, tmp_path):
    repo, p = await _parent(tmp_path)
    p_token = _token_in_process(p)
    child = await _start(3591, repo, "subagent: tests", parent_session_id=p,
                         ticket_role="contributor")
    assert child != p
    assert mcp._IN_PROCESS_SESSION_ID == p                     # default unchanged
    assert sp.resolve_pointer() == p                           # pointer unchanged
    assert _token_in_process(p) == p_token                     # capability unchanged
    ring = _keyring()
    assert ring[p]["token"] == p_token and ring[p]["parent"] is None
    assert ring[child]["parent"] == p and ring[child]["state"] == "open"
    assert ring[child]["token"] not in ("", p_token)
    row = await _row(wired, child)
    assert row["agent"] == (await _row(wired, p))["agent"]     # same Claude session
    assert f"subagent of session {p}" in row["description"]
    assert (await _row(wired, p))["status"] == "active"        # parent record kept


def _live_mcp(live_ats, monkeypatch, tmp_path):
    """The real MCP handlers against a real ats-server process (file database,
    concurrent requests). No ticket: the server process has no Echo stub."""
    url, client, _env = live_ats
    monkeypatch.setattr(mcp, "SERVER_URL", url)
    _declare_identity(monkeypatch)
    _fresh_process(monkeypatch, tmp_path)
    monkeypatch.setenv("ATS_SERVER_URL", url)
    repo = tmp_path / "repo"
    repo.mkdir(exist_ok=True)
    return client, repo


async def _start_live(repo, description, scope, **extra):
    text = await _call("start_session", description=description, scope=scope,
                       repo_root=str(repo), **extra)
    assert "Session started" in text, text
    return text.split("Session ID: ")[1].split()[0]


def test_item2_parent_and_several_subagents_start_concurrently(
        live_ats, tmp_path, monkeypatch):
    client, repo = _live_mcp(live_ats, monkeypatch, tmp_path)

    async def go():
        p = await _start_live(repo, "parent", ["src/parent.py"])
        p_token = _token_in_process(p)
        children = await asyncio.gather(*(
            _start_live(repo, f"subagent {i}", [f"src/sub{i}.py"], parent_session_id=p)
            for i in range(4)))
        for c in children:
            text = await _call("log_decision", session_id=c, title=f"by {c}", chosen="x",
                               reasoning="y")
            assert "Decision logged" in text, text
        return p, p_token, children

    p, p_token, children = asyncio.run(go())
    assert len(set(children)) == 4
    ring = _keyring()
    assert set(ring) == {p, *children}
    assert all(ring[c]["parent"] == p and ring[c]["state"] == "open" for c in children)
    assert mcp._IN_PROCESS_SESSION_ID == p and sp.resolve_pointer() == p
    assert ring[p]["token"] == p_token
    for c in children:
        titles = [d["title"] for d in client.get(
            "/api/decisions", params={"session_id": c}).json()]
        assert f"by {c}" in titles, titles   # recorded on exactly that subagent
        assert not any(t.startswith("by ") and t != f"by {c}" for t in titles), titles


@pytest.mark.asyncio
async def test_item2_child_completion_leaves_the_parent_capability_and_pointer_intact(
        wired, tmp_path):
    repo, p = await _parent(tmp_path)
    child = await _start(3591, repo, "subagent", parent_session_id=p,
                         ticket_role="contributor")
    p_locks = await _locks_of(wired, p)
    text = await _call("complete_session", session_id=child, summary="subagent done")
    assert "Session completed" in text, text
    assert "default session is now" not in text
    assert sp.resolve_pointer() == p and mcp._IN_PROCESS_SESSION_ID == p
    assert _keyring()[p]["state"] == "open" and _keyring()[child]["state"] == "completed"
    assert (await _row(wired, p))["status"] == "active"
    # NO automatic lock transfer: the child's locks were released, the parent's
    # are exactly what they were, and none moved between them.
    assert await _locks_of(wired, child) == []
    assert await _locks_of(wired, p) == p_locks
    text = await _call("complete_session", summary="parent done")   # the default: p
    assert "Session completed" in text and p in text, text


@pytest.mark.asyncio
async def test_item2_parent_completion_never_hands_its_locks_or_default_to_a_child(
        wired, tmp_path):
    repo, p = await _parent(tmp_path)
    child = await _start(3591, repo, "subagent", parent_session_id=p,
                         ticket_role="contributor")
    c_locks = await _locks_of(wired, child)
    text = await _call("complete_session", session_id=p, summary="parent done")
    assert "Session completed" in text, text
    assert "default session is now" not in text      # a subagent never becomes default
    assert await _locks_of(wired, p) == []
    assert await _locks_of(wired, child) == c_locks
    assert (await _row(wired, child))["status"] == "active"
    text = await _call("complete_session", session_id=child, summary="child done")
    assert "Session completed" in text, text


@pytest.mark.asyncio
async def test_item2_sequential_sessions_keep_earlier_capabilities(wired, tmp_path):
    repo, first = await _parent(tmp_path)
    await _call("complete_session", summary="first done")
    second = await _start(3591, repo, "second, after the first completed")
    ring = _keyring()
    assert ring[first]["state"] == "completed" and ring[second]["state"] == "open"
    assert sp.load_approval_token(first, cid=CID) == _token_in_process(first) != ""
    assert mcp._IN_PROCESS_SESSION_ID == second


@pytest.mark.asyncio
@pytest.mark.parametrize("parent", ["unheld", "completed", "foreign"])
async def test_item2_a_subagent_registers_only_under_an_open_parent_it_holds(
        wired, tmp_path, parent):
    repo, p = await _parent(tmp_path)
    if parent == "completed":
        await _call("complete_session", session_id=p, summary="done")
        target = p
    elif parent == "foreign":
        target = (await _rest(wired, "post", "/api/sessions", json={
            "developer": "dev", "agent": "claude-code:7ff8070e", "scope": [],
            "description": "same label, not held"})).json()["id"]
    else:
        target = str(uuid.uuid4())
    before = len((await _rest(wired, "get", "/api/sessions")).json())
    text = await _call("start_session", description="sub", scope=["x.py"],
                       repo_root=str(repo), parent_session_id=target)
    assert text.startswith("❌") and "Subagent session not started" in text, text
    assert len((await _rest(wired, "get", "/api/sessions")).json()) == before


@pytest.mark.asyncio
async def test_item2_without_explicit_registration_a_second_start_is_not_a_subagent(
        wired, tmp_path):
    """The shared Claude session id alone never makes a session a subagent: a
    plain second start_session is an ordinary sequential/parallel session and,
    as before, becomes the default."""
    a, b = await _a_then_b(tmp_path)
    assert mcp._IN_PROCESS_SESSION_ID == b and sp.resolve_pointer() == b
    assert _keyring()[b]["parent"] is None and b not in mcp._IN_PROCESS_PARENT


@pytest.mark.asyncio
async def test_item2_two_repository_bound_sessions_in_one_claude_process(wired, tmp_path):
    a, b = await _a_then_b(tmp_path)
    text = await _call("extend_scope", session_id=a, patterns=["docs/a/**"])
    assert "Scope extended" in text, text
    assert "docs/a/**" in (await _row(wired, a))["scope"]
    assert "docs/a/**" not in (await _row(wired, b))["scope"]
    assert (await _row(wired, a))["repo_root"] != (await _row(wired, b))["repo_root"]


def _run_edit_guard(monkeypatch, capsys, cid, file_path, repo):
    from ai_team_sync.hooks import pre_tool_use_lockcheck as guard

    monkeypatch.setattr(guard, "_is_noise", lambda _p: False)
    monkeypatch.setattr(guard, "_roots", lambda _p: (str(repo), str(repo)))
    monkeypatch.setattr(guard, "_coordinated_roots", lambda: [str(repo)])
    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps({
        "tool_name": "Write", "session_id": cid, "tool_input": {"file_path": file_path}})))
    with pytest.raises(SystemExit) as stopped:
        guard.main()
    return stopped.value.code, capsys.readouterr().err


def test_item2_edit_guard_grants_a_registered_subagents_lock_never_a_label_lookalike(
        live_ats, tmp_path, monkeypatch, capsys):
    """Real server, real MCP handlers, real hook. The parent keeps the pointer;
    the registered subagent's lock grants its edit (sibling rule); a row that
    merely carries the same 'claude-code:<cid8>' label -- the shared Claude
    session id, all a forger or a colliding session needs -- grants nothing,
    because this Claude session's keyring does not hold its capability."""
    client, repo = _live_mcp(live_ats, monkeypatch, tmp_path)

    async def go():
        p = await _start_live(repo, "parent", ["parent.py"])
        child = await _start_live(repo, "subagent", ["sub.py"], parent_session_id=p)
        return p, child

    p, child = asyncio.run(go())
    assert sp.resolve_pointer() == p
    code, err = _run_edit_guard(monkeypatch, capsys, CID, str(repo / "sub.py"), repo)
    assert code == 0, err
    lookalike = client.post("/api/sessions", json={
        "developer": "dev", "agent": f"claude-code:{CID[:8]}", "scope": ["forged.py"],
        "repo_root": str(repo), "description": "same label, not registered here",
        "auto_lock": True})
    assert lookalike.status_code == 201, lookalike.text
    code, err = _run_edit_guard(monkeypatch, capsys, CID, str(repo / "forged.py"), repo)
    assert code == 2 and "holds no live lock" in err, err
