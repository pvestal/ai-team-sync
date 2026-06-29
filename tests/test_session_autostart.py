"""SessionStart auto-registration + per-session pointer (Gap 1 + Gap 3).

Auto-register means a Claude session shows up in team_status with ZERO manual
start_session. The per-session pointer (keyed by CLAUDE_CODE_SESSION_ID) means
concurrent sessions no longer clobber each other's ~/.ats_session pointer.

Routes the hook's httpx calls into the in-process ASGI app (same pattern as
test_mcp_extend_scope) so creation is exercised end-to-end against the DB.
"""
from __future__ import annotations

import pytest

from ai_team_sync import session_pointer as sp
from ai_team_sync.hooks import session_autostart as autostart


@pytest.fixture(autouse=True)
def _isolated_state(tmp_path, monkeypatch):
    # Pointer files land in a tmp dir, never the real ~/.ats_session.
    monkeypatch.setenv("ATS_STATE_DIR", str(tmp_path))
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", "deadbeef-1111-2222-3333-444455556666")
    monkeypatch.setenv("ATS_DEVELOPER", "patrick")
    monkeypatch.delenv("ATS_SESSION_ID", raising=False)
    return tmp_path


async def _active_sessions(client):
    r = await client.get("/api/sessions", params={"status": "active"})
    assert r.status_code == 200
    return r.json()


@pytest.mark.asyncio
async def test_autostart_creates_session_visible_in_team_status(client):
    before = await _active_sessions(client)
    assert len(before) == 0

    sid = await autostart.ensure_session("http://test", client)

    assert sid
    after = await _active_sessions(client)
    assert len(after) == 1
    row = after[0]
    assert row["developer"] == "patrick"
    assert row["agent"].startswith("claude-code:")
    assert row["scope"] == []  # auto-register claims no locks
    # Pointer recorded so heartbeat/complete act on THIS session.
    assert sp.resolve_pointer() == sid


@pytest.mark.asyncio
async def test_autostart_is_idempotent_across_refires(client):
    # SessionStart fires again on resume/compaction — must NOT spawn a second row.
    sid1 = await autostart.ensure_session("http://test", client)
    sid2 = await autostart.ensure_session("http://test", client)

    assert sid1 == sid2
    active = await _active_sessions(client)
    assert len(active) == 1


@pytest.mark.asyncio
async def test_no_claude_session_id_fails_open(client, monkeypatch):
    # Without a session id we can't key a pointer — fall back to manual, never crash.
    monkeypatch.delenv("CLAUDE_CODE_SESSION_ID", raising=False)
    monkeypatch.delenv("ATS_SESSION", raising=False)
    sid = await autostart.ensure_session("http://test", client)
    assert sid is None
    assert len(await _active_sessions(client)) == 0


def test_per_session_pointer_beats_global_clobber(_isolated_state, monkeypatch):
    # The Gap 3 fix: two concurrent sessions write the same global file, but each
    # resolves its OWN id via the per-session pointer.
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", "aaaaaaaa-0000")
    sp.save_pointer("session-A")
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", "bbbbbbbb-0000")
    sp.save_pointer("session-B")  # clobbers the global file with B

    # A still resolves to A from its per-session pointer, not the clobbered global.
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", "aaaaaaaa-0000")
    assert sp.resolve_pointer() == "session-A"
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", "bbbbbbbb-0000")
    assert sp.resolve_pointer() == "session-B"


def test_explicit_env_override_wins(_isolated_state, monkeypatch):
    sp.save_pointer("from-file")
    monkeypatch.setenv("ATS_SESSION_ID", "from-env")
    assert sp.resolve_pointer() == "from-env"
