"""ats-lockcheck-repo-anchoring-p01: repo-relative scope patterns must not
collide across repos.

Observed 2026-07-04 (twice in one session): a 'tests/**' lock held for
/opt/anime-studio hard-blocked writes to ~/code/ai-team-sync/tests/. Sessions
now carry repo_root; every matching surface skips locks anchored to a
different repo. '' on either side = legacy match-everywhere (conservative).
"""

from __future__ import annotations

import pytest

from ai_team_sync.hooks.pre_tool_use_lockcheck import find_conflicts
from ai_team_sync.routers.locks import _cross_repo


# ---------------------------------------------------------------------------
# _cross_repo (pure)
# ---------------------------------------------------------------------------

def test_cross_repo_both_anchored_differ():
    assert _cross_repo("/opt/anime-studio", "/home/p/code/ai-team-sync") is True


def test_cross_repo_same_root():
    assert _cross_repo("/opt/anime-studio", "/opt/anime-studio/") is False


def test_cross_repo_legacy_unanchored():
    assert _cross_repo("", "/opt/anime-studio") is False
    assert _cross_repo("/opt/anime-studio", "") is False
    assert _cross_repo("", "") is False


# ---------------------------------------------------------------------------
# hook: find_conflicts anchoring
# ---------------------------------------------------------------------------

def _sess(**over):
    base = {
        "status": "active",
        "agent": "claude-code:e4a6e21c",
        "scope": ["tests/**"],
        "description": "other session",
        "repo_root": "/opt/anime-studio",
    }
    base.update(over)
    return base


def test_hook_skips_other_repo_lock():
    # The exact observed false positive: anime-studio 'tests/**' vs an
    # ai-team-sync test file.
    conflicts = find_conflicts(
        "tests/test_override_push.py", [_sess()], "me000000",
        file_repo_root="/home/p/code/ai-team-sync",
    )
    assert conflicts == []


def test_hook_still_blocks_same_repo():
    conflicts = find_conflicts(
        "tests/test_x.py", [_sess()], "me000000",
        file_repo_root="/opt/anime-studio",
    )
    assert len(conflicts) == 1


def test_hook_legacy_unanchored_session_still_blocks():
    # Session without repo_root (pre-upgrade row): conservative legacy match.
    conflicts = find_conflicts(
        "tests/test_x.py", [_sess(repo_root="")], "me000000",
        file_repo_root="/home/p/code/ai-team-sync",
    )
    assert len(conflicts) == 1


def test_hook_unanchored_file_still_blocks():
    # File outside any git repo: conservative legacy match.
    conflicts = find_conflicts(
        "tests/test_x.py", [_sess()], "me000000", file_repo_root="",
    )
    assert len(conflicts) == 1


# ---------------------------------------------------------------------------
# API surfaces
# ---------------------------------------------------------------------------

async def _anchored_session(client, repo_root: str, scope=("tests/**",)):
    resp = await client.post("/api/sessions", json={
        "developer": "anchor-dev", "scope": list(scope),
        "description": "anchored", "auto_lock": True, "repo_root": repo_root,
    })
    assert resp.status_code in (200, 201), resp.text
    return resp.json()


@pytest.mark.asyncio
async def test_session_repo_root_roundtrip(client):
    s = await _anchored_session(client, "/repo/a")
    assert s["repo_root"] == "/repo/a"
    got = (await client.get(f"/api/sessions/{s['id']}")).json()
    assert got["repo_root"] == "/repo/a"


@pytest.mark.asyncio
async def test_lock_check_skips_other_repo(client):
    await _anchored_session(client, "/repo/a")
    r = await client.post("/api/locks/check", json={
        "paths": ["tests/test_x.py"], "repo_root": "/repo/b"})
    assert all(not row["locked"] for row in r.json())


@pytest.mark.asyncio
async def test_lock_check_same_repo_still_locks(client):
    await _anchored_session(client, "/repo/a")
    r = await client.post("/api/locks/check", json={
        "paths": ["tests/test_x.py"], "repo_root": "/repo/a"})
    assert any(row["locked"] for row in r.json())


@pytest.mark.asyncio
async def test_lock_check_legacy_caller_still_locks(client):
    await _anchored_session(client, "/repo/a")
    r = await client.post("/api/locks/check", json={"paths": ["tests/test_x.py"]})
    assert any(row["locked"] for row in r.json())


@pytest.mark.asyncio
async def test_session_create_no_conflict_across_repos(client):
    await _anchored_session(client, "/repo/a")
    # Same patterns, DIFFERENT repo: must not 409.
    resp = await client.post("/api/sessions", json={
        "developer": "dev-b", "scope": ["tests/**"],
        "description": "other repo, same pattern", "auto_lock": True,
        "repo_root": "/repo/b",
    })
    assert resp.status_code in (200, 201), resp.text


@pytest.mark.asyncio
async def test_precommit_check_skips_other_repo(client):
    await _anchored_session(client, "/repo/a")
    r = await client.post("/api/git/pre-commit-check", json={
        "staged_files": ["tests/test_x.py"], "repo_root": "/repo/b"})
    body = r.json()
    assert body["can_proceed"] is True
    assert body["advisory_locks"] == [] and body["blocking_locks"] == []
