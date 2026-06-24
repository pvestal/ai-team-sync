"""Slice 2 — hook-driven auto-emit: HTTP presence endpoint + the PostToolUse hook's
pure payload builder.
"""
from __future__ import annotations

import pytest

from ai_team_sync.hooks.post_tool_use_presence import build_presence
from ai_team_sync.presence import store


@pytest.fixture(autouse=True)
def _clear_store():
    store._devs.clear()
    yield
    store._devs.clear()


# --- HTTP endpoint ---

@pytest.mark.asyncio
async def test_post_presence_updates_and_lists(client):
    resp = await client.post("/api/presence", json={
        "developer": "alice", "agent": "claude-code",
        "files": ["src/auth/jwt.py"], "intent": "rewriting token validation",
    })
    assert resp.status_code == 200
    body = resp.json()
    assert body[0]["developer"] == "alice"
    assert body[0]["intent"] == "rewriting token validation"

    listed = (await client.get("/api/presence")).json()
    assert listed[0]["files"] == ["src/auth/jwt.py"]


@pytest.mark.asyncio
async def test_post_presence_intent_optional(client):
    resp = await client.post("/api/presence", json={
        "developer": "bob", "agent": "cursor", "files": ["a.py"]})
    assert resp.status_code == 200
    assert resp.json()[0]["intent"] == ""


# --- hook payload builder (pure) ---

def test_build_presence_for_edit():
    body = build_presence(
        {"tool_name": "Edit", "tool_input": {"file_path": "/repo/src/x.py"}, "cwd": "/repo"},
        {"ATS_AGENT": "claude-code", "ATS_INTENT": "fix bug"},
    )
    assert body == {"developer": body["developer"], "agent": "claude-code",
                    "files": ["src/x.py"], "intent": "fix bug"}
    assert body["files"] == ["src/x.py"]  # relativized against cwd


def test_build_presence_skips_non_edit_tools():
    assert build_presence({"tool_name": "Bash", "tool_input": {"command": "ls"}}, {}) is None
    assert build_presence({"tool_name": "Read", "tool_input": {"file_path": "x.py"}}, {}) is None


def test_build_presence_skips_when_no_file_path():
    assert build_presence({"tool_name": "Edit", "tool_input": {}}, {}) is None


def test_build_presence_developer_from_env():
    body = build_presence(
        {"tool_name": "Write", "tool_input": {"file_path": "y.py"}},
        {"ATS_DEVELOPER": "carol"},
    )
    assert body["developer"] == "carol"
    assert body["agent"] == "claude-code"   # default
    assert body["files"] == ["y.py"]
