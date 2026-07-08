"""Per-session uncommitted-diff visibility (ats-git-diff-merge-workflow-p01).

GET /api/sessions surfaces each active session's uncommitted files that fall
inside its scope, so overlap between sessions is visible as diffs on the
board instead of only as lock patterns.
"""
from __future__ import annotations

import subprocess

import pytest


def _git_repo_with_uncommitted(tmp_path, files):
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    subprocess.run(["git", "-c", "user.email=t@t", "-c", "user.name=t",
                    "commit", "-q", "--allow-empty", "-m", "base"], cwd=repo, check=True)
    for rel in files:
        p = repo / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text("dirty\n")
    return repo


@pytest.mark.asyncio
async def test_sessions_list_shows_uncommitted_in_scope(client, tmp_path):
    repo = _git_repo_with_uncommitted(tmp_path, ["src/a.py", "docs/readme.md"])

    resp = await client.post("/api/sessions", json={
        "developer": "tester",
        "agent": "claude",
        "scope": ["src/**"],
        "description": "diff visibility test",
        "repo_root": str(repo),
    })
    assert resp.status_code in (200, 201), resp.text

    listed = await client.get("/api/sessions", params={"status": "active"})
    assert listed.status_code == 200
    (session,) = [s for s in listed.json() if s["developer"] == "tester"]
    # src/a.py is uncommitted AND in scope; docs/readme.md is out of scope.
    assert session["uncommitted_in_scope"] == ["src/a.py"]


@pytest.mark.asyncio
async def test_sessions_list_unanchored_session_empty_visibility(client):
    resp = await client.post("/api/sessions", json={
        "developer": "tester2",
        "agent": "claude",
        "scope": ["src/**"],
        "description": "no repo_root (legacy)",
    })
    assert resp.status_code in (200, 201), resp.text

    listed = await client.get("/api/sessions", params={"status": "active"})
    (session,) = [s for s in listed.json() if s["developer"] == "tester2"]
    assert session["uncommitted_in_scope"] == []
