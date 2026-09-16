"""#2757 at the surface an agent actually reads.

The API-level invariant is pinned in test_caller_identity_lock_exclusion_2757.
These drive the MCP tools, because that is where the defect was observed twice:
a session told its own exclusive claims blocked its own commit (#2756), and a
session handed its own brand-new locks under BLOCKERS NOW by the very call that
created them (2026-09-15, session 048705db).
"""
from __future__ import annotations

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

import ai_team_sync.mcp.server as mcp
from ai_team_sync.database import get_db
from ai_team_sync.server import create_app

ROOT = "/srv/echo-2757-mcp"


def _wire(monkeypatch, db_engine, tmp_path):
    """MCP tools against an in-process app, with pointer state in a temp dir so
    a test never touches the operator's real ~/.ats_session.

    `_IN_PROCESS_SESSION_ID` is patched to its own default so pytest restores it:
    the start_session TOOL sets that module global and nothing resets it, so a
    test that claims a session would otherwise be the "active session" for every
    later test in the process (caught here: it made test_mcp_extend_scope talk to
    the real :8400 server). Pre-existing global state, isolated rather than
    worked around."""
    monkeypatch.setenv("ATS_STATE_DIR", str(tmp_path))
    monkeypatch.setattr(mcp, "_IN_PROCESS_SESSION_ID", None)
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
    return transport


async def _foreign(transport, scope, mode="exclusive", agent="codex:theirs"):
    async with AsyncClient(transport=transport, base_url="http://localhost:8400") as c:
        r = await c.post("/api/sessions", json={
            "developer": "alice", "agent": agent, "scope": list(scope),
            "repo_root": ROOT, "description": "foreign", "auto_lock": True,
            "lock_mode": mode})
        assert r.status_code == 201, r.text
        return r.json()["id"]


# ── the brief a claim returns ──────────────────────────────────────────────

@pytest.mark.asyncio
async def test_start_session_brief_does_not_list_its_own_new_locks(
        db_engine, monkeypatch, tmp_path):
    _wire(monkeypatch, db_engine, tmp_path)

    out = await mcp.call_tool("start_session", {
        "scope": ["docs/**"], "description": "2757 own-lock brief", "repo_root": ROOT})
    text = out[0].text

    assert "Locks created: 1" in text
    assert "BLOCKERS NOW" not in text, text


@pytest.mark.asyncio
async def test_start_session_brief_still_shows_a_foreign_blocker(
        db_engine, monkeypatch, tmp_path):
    transport = _wire(monkeypatch, db_engine, tmp_path)
    await _foreign(transport, ["docs/**"], mode="advisory")

    out = await mcp.call_tool("start_session", {
        "scope": ["docs/**"], "description": "2757 foreign blocker", "repo_root": ROOT})
    text = out[0].text

    assert "BLOCKERS NOW" in text
    assert "codex:theirs" in text
    assert text.count("'docs/**' is claimed by") == 1, text


# ── the commit verdict ─────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_pre_commit_check_does_not_block_on_the_callers_own_lock(
        db_engine, monkeypatch, tmp_path):
    transport = _wire(monkeypatch, db_engine, tmp_path)
    mine = await _foreign(transport, ["src/auth/**"], agent="claude-code:mine")
    monkeypatch.setattr(mcp, "load_session_id", lambda: mine)

    out = await mcp.call_tool("pre_commit_check",
                              {"paths": ["src/auth/jwt.py"], "repo_root": ROOT})

    assert "All files clear" in out[0].text


@pytest.mark.asyncio
async def test_pre_commit_check_names_the_agent_and_session_of_a_foreign_lock(
        db_engine, monkeypatch, tmp_path):
    transport = _wire(monkeypatch, db_engine, tmp_path)
    theirs = await _foreign(transport, ["src/auth/**"])
    mine = await _foreign(transport, ["docs/**"], agent="claude-code:mine")
    monkeypatch.setattr(mcp, "load_session_id", lambda: mine)

    out = await mcp.call_tool("pre_commit_check",
                              {"paths": ["src/auth/jwt.py"], "repo_root": ROOT})
    text = out[0].text

    assert "BLOCKED" in text
    assert "codex:theirs" in text
    assert theirs in text          # the WHOLE id: a prefix is not actionable


@pytest.mark.asyncio
async def test_pre_commit_check_states_when_the_caller_is_unresolved(
        db_engine, monkeypatch, tmp_path):
    transport = _wire(monkeypatch, db_engine, tmp_path)
    await _foreign(transport, ["src/auth/**"])
    monkeypatch.setattr(mcp, "load_session_id", lambda: None)

    out = await mcp.call_tool("pre_commit_check",
                              {"paths": ["src/auth/jwt.py"], "repo_root": ROOT})
    text = out[0].text

    assert "BLOCKED" in text
    assert "own locks" in text, text
