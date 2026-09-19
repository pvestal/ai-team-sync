"""Observed file actions carry the reporting session, never just its operator."""

from __future__ import annotations

import pytest

from ai_team_sync.hooks.post_tool_use_presence import build_activity


@pytest.mark.asyncio
async def test_observed_read_and_edit_are_attributed_to_session(client):
    response = await client.post("/api/sessions", json={
        "developer": "pvestal", "agent": "codex:12345678", "scope": [],
    })
    session = response.json()
    token = response.headers["X-ATS-Approval-Token"]
    sid = session["id"]
    denied = await client.post("/api/file-activities", json={
        "session_id": sid, "action": "read", "path": "src/a.py"})
    assert denied.status_code == 403
    for action in ("read", "edit"):
        created = await client.post("/api/file-activities", json={
            "session_id": sid, "action": action, "path": "src/a.py",
            "repo_root": "/repo"}, headers={"X-ATS-Approval-Token": token})
        assert created.status_code == 201, created.text
        assert created.json()["agent"] == "codex:12345678"
        assert created.json()["developer"] == "pvestal"
    listed = (await client.get("/api/file-activities", params={"session_id": sid})).json()
    assert len(listed) == 2
    assert {r["action"] for r in listed} == {"read", "edit"}
    assert {r["session_id"] for r in listed} == {sid}


def test_hook_reports_only_instrumented_file_actions():
    read = build_activity({"tool_name": "Read", "tool_input": {
        "file_path": "/repo/src/a.py"}, "session_id": "claude-cid", "cwd": "/repo"},
        {"ATS_SESSION_ID": "ats-session"})
    assert read == {"session_id": "ats-session", "action": "read",
                    "path": "src/a.py", "repo_root": ""}
    assert build_activity({"tool_name": "Bash", "tool_input": {
        "command": "cat src/a.py"}}, {"ATS_SESSION_ID": "ats-session"}) is None
