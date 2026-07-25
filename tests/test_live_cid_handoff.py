"""The /clear session-id rotation that made the lock guard block its own session.

Measured on tower 2026-07-25 (Tower #2003). A Claude process spawns the stdio MCP
server once, at startup. `/clear` (and resume/compact) rotates
CLAUDE_CODE_SESSION_ID for every subsequently-spawned HOOK, but the long-lived MCP
child keeps whatever value it was spawned with, forever:

    ats-mcp pid 1891887 (spawned 11:35) -> CLAUDE_CODE_SESSION_ID=05a1328e-...
    live hook env, same Claude process   -> CLAUDE_CODE_SESSION_ID=6c0fb7b2-...

pre_tool_use_lockcheck skips "my own session" by testing whether the live payload
session_id's 8-char prefix appears in a row's agent label, and those labels are
built by the MCP from its cached env. Post-/clear the prefixes never match, so a
session that declares scope through the MCP hard-blocks its own edits — and no
lock-level reap helps, because the guard reads session SCOPE, not lock rows. The
documented escape hatch (ATS_LOCKCHECK_BLOCK=0) disables the guard for genuine
third-party conflicts too.

The one identifier both sides still agree on is the owning Claude process id:
hooks carry it in $CLAUDE_PID, and the MCP is a direct child of that process, so
os.getppid() is the same number. The SessionStart hook — which always runs with
the live cid — publishes it under that key, and the MCP reads it instead of
trusting its own stale environment.
"""
from __future__ import annotations

import json
import os

import pytest

from ai_team_sync import session_pointer as sp

STALE = "05a1328e-d7c8-4557-997c-6411858a2bcd"  # MCP's spawn-time env, pre-/clear
LIVE = "6c0fb7b2-d717-420e-aa03-a4566517a69c"   # what hooks see after /clear


@pytest.fixture(autouse=True)
def _isolated_state(tmp_path, monkeypatch):
    monkeypatch.setenv("ATS_STATE_DIR", str(tmp_path))
    monkeypatch.delenv("ATS_SESSION", raising=False)
    monkeypatch.delenv("ATS_SESSION_ID", raising=False)
    yield


def test_host_pid_prefers_claude_pid_env(monkeypatch):
    """Hooks get CLAUDE_PID; it names the Claude process, not the hook itself."""
    monkeypatch.setenv("CLAUDE_PID", "1891656")
    assert sp.host_pid() == "1891656"


def test_host_pid_falls_back_to_parent_for_the_mcp(monkeypatch):
    """The MCP subprocess has no CLAUDE_PID, but its parent IS the Claude process."""
    monkeypatch.delenv("CLAUDE_PID", raising=False)
    assert sp.host_pid() == str(os.getppid())


def test_mcp_prefers_the_cid_the_hook_published(monkeypatch):
    """The bug, end to end: stale env on the MCP, live cid published by the hook."""
    monkeypatch.setenv("CLAUDE_PID", "1891656")
    sp.publish_live_cid(LIVE)

    # Now read it back the way the MCP subprocess would: same host, stale env.
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", STALE)
    assert sp.claude_session_id() == LIVE


def test_falls_back_to_env_when_nothing_published(monkeypatch):
    """No publisher (hook disabled, other agent) must behave exactly as before."""
    monkeypatch.setenv("CLAUDE_PID", "1891656")
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", STALE)
    assert sp.claude_session_id() == STALE


def test_published_cid_is_scoped_to_its_host_process(monkeypatch):
    """Another Claude process on the same box must not pick up my cid."""
    monkeypatch.setenv("CLAUDE_PID", "1891656")
    sp.publish_live_cid(LIVE)

    monkeypatch.setenv("CLAUDE_PID", "2466541")  # a different Claude process
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", STALE)
    assert sp.claude_session_id() == STALE


def test_recycled_pid_does_not_resurrect_a_dead_sessions_cid(monkeypatch, tmp_path):
    """PIDs get reused. A record whose host process start-time no longer matches
    must be ignored, or a brand-new session inherits a dead one's identity."""
    monkeypatch.setenv("CLAUDE_PID", "1891656")
    sp.publish_live_cid(LIVE)

    # Forge a start-time that cannot match the live process behind that pid.
    path = sp.live_cid_path("1891656")
    record = json.loads(path.read_text())
    record["starttime"] = "-1"
    path.write_text(json.dumps(record))

    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", STALE)
    assert sp.claude_session_id() == STALE


def test_agent_label_uses_the_live_cid(monkeypatch):
    """The label is what the lock guard string-matches on — it must carry the
    cid the hook will present, not the MCP's spawn-time one."""
    from ai_team_sync.mcp import server as mcp_server

    monkeypatch.setenv("CLAUDE_PID", "1891656")
    monkeypatch.setenv("ATS_AGENT", "claude-code")
    sp.publish_live_cid(LIVE)
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", STALE)

    assert mcp_server.session_agent_label() == f"claude-code:{LIVE[:8]}"


def test_lock_guard_no_longer_self_blocks_after_clear(monkeypatch):
    """The actual regression: a session that declared scope via the MCP must not
    block the very hook invocation that shares its Claude process."""
    from ai_team_sync.hooks import pre_tool_use_lockcheck as guard
    from ai_team_sync.mcp import server as mcp_server

    monkeypatch.setenv("CLAUDE_PID", "1891656")
    monkeypatch.setenv("ATS_AGENT", "claude-code")
    sp.publish_live_cid(LIVE)
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", STALE)

    my_row = {
        "status": "active",
        "agent": mcp_server.session_agent_label(),
        "scope": ["packages/core/migrations/**"],
        "description": "#2000: make the migrations golden fixture deterministic",
        "repo_root": "/opt/anime-studio",
    }
    conflicts = guard.find_conflicts(
        "packages/core/migrations/scene_cohort.py", [my_row],
        my_session_id=LIVE, file_repo_root="/opt/anime-studio",
    )
    assert conflicts == []


def test_lock_guard_still_blocks_a_genuinely_different_session(monkeypatch):
    """Guard rail: the fix must not turn every conflict into a self-conflict."""
    from ai_team_sync.hooks import pre_tool_use_lockcheck as guard

    other = {
        "status": "active",
        "agent": "claude-code:1177bf5e",
        "scope": ["packages/core/migrations/**"],
        "description": "someone else's work",
        "repo_root": "/opt/anime-studio",
    }
    conflicts = guard.find_conflicts(
        "packages/core/migrations/scene_cohort.py", [other],
        my_session_id=LIVE, file_repo_root="/opt/anime-studio",
    )
    assert len(conflicts) == 1
