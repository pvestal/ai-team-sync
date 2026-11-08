"""The MCP `delegate` tool must delegate as ITS caller, never as the shared pointer.

Observed 2026-10-08, delegation 74960483: a Codex controller's ats-mcp had no
in-process session, the `ats delegate` subprocess fell back to the shared
~/.ats_session, and the delegation was recorded under a Claude session
(parent_owner 1feefbd0, delegating_worker claude-code:6bb9adc5). Every other
mutating tool already refuses a 'global' identity; delegate shelled out first.
"""
from __future__ import annotations

import subprocess

import pytest

import ai_team_sync.mcp.server as mcp

ARGS = {"objective": "review one diff", "acceptance": "PASS or BLOCK",
        "mode": "VERIFY", "worker": "claude-code", "lease_minutes": 1}


class _NoNetwork:
    """Heartbeat target; delegate itself must not need the network to decide."""

    def __init__(self, *a, **k):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def post(self, *a, **k):
        return None

    async def get(self, *a, **k):
        raise AssertionError("delegate consulted the server to decide identity")


@pytest.fixture
def spawned(monkeypatch, tmp_path):
    monkeypatch.setenv("ATS_STATE_DIR", str(tmp_path))
    for var in ("ATS_AGENT", "ATS_DELEGATION", "CLAUDECODE", "CLAUDE_CODE",
                "CLAUDE_CODE_SESSION_ID"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setattr(mcp, "_IN_PROCESS_SESSION_ID", None)
    monkeypatch.setattr(mcp, "_IN_PROCESS_TOKENS", {})
    monkeypatch.setattr(mcp, "_IN_PROCESS_STATE", {})
    monkeypatch.setattr(mcp, "_IN_PROCESS_PARENT", {})
    monkeypatch.setattr(mcp, "_CAPABILITY_PROBLEMS", {})
    monkeypatch.setattr(mcp.httpx, "AsyncClient", _NoNetwork)
    calls = []

    def fake_run(argv, *a, **k):
        calls.append(argv)
        return subprocess.CompletedProcess(argv, 0, stdout="{}", stderr="")

    monkeypatch.setattr(subprocess, "run", fake_run)
    return calls


def _text(result) -> str:
    return "\n".join(part.text for part in result)


async def test_delegate_refuses_a_parent_taken_from_the_shared_pointer(monkeypatch, spawned):
    monkeypatch.setattr(mcp, "resolve_identity", lambda: ("someone-elses-sid", "global"))

    out = _text(await mcp._call_tool_impl("delegate", dict(ARGS)))

    assert spawned == [], "nothing may be launched under a borrowed identity"
    assert "SHARED" in out


async def test_delegate_passes_its_own_session_as_the_explicit_parent(monkeypatch, spawned):
    monkeypatch.setattr(mcp, "resolve_identity", lambda: ("my-sid", "in_process"))
    mcp._remember_in_process("my-sid", "my-capability")

    await mcp._call_tool_impl("delegate", dict(ARGS))

    assert len(spawned) == 1
    argv = spawned[0]
    assert argv[argv.index("--parent-session") + 1] == "my-sid"
