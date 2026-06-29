"""Slice 2 — hook-driven auto-emit: HTTP presence endpoint + the PostToolUse hook's
pure payload builder.
"""
from __future__ import annotations

import pytest

from ai_team_sync.hooks.post_tool_use_presence import build_presence, _display_path
from ai_team_sync.presence import store
from ai_team_sync.routers.presence_http import _path_matches


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


@pytest.mark.parametrize("path", [
    "/tmp/claude-xyz/scratch.txt",
    "/home/me/proj/.git/COMMIT_EDITMSG",
    "/home/me/proj/node_modules/x/index.js",
    "/home/me/proj/scratchpad/probe.txt",
    "/home/me/.claude/settings.json",
])
def test_build_presence_skips_noise_paths(path):
    assert build_presence({"tool_name": "Edit", "tool_input": {"file_path": path}}, {}) is None


def test_display_path_is_repo_relative_for_in_repo_file(tmp_path):
    # a file inside a git repo renders relative to the repo root, not ../../ junk
    (tmp_path / ".git").mkdir()
    f = tmp_path / "src" / "mod.py"
    f.parent.mkdir(parents=True)
    f.write_text("x")
    assert _display_path(str(f), cwd="/somewhere/else") == "src/mod.py"


def test_display_path_basename_when_outside_cwd_and_no_repo(tmp_path):
    # no .git anywhere up-tree, and path escapes cwd -> bare basename, never ../../
    f = tmp_path / "loose.txt"
    f.write_text("x")
    out = _display_path(str(f), cwd="/completely/unrelated")
    assert out == "loose.txt"


def test_build_presence_developer_from_env():
    body = build_presence(
        {"tool_name": "Write", "tool_input": {"file_path": "y.py"}},
        {"ATS_DEVELOPER": "carol"},
    )
    assert body["developer"] == "carol"
    assert body["agent"] == "claude-code"   # default
    assert body["files"] == ["y.py"]


# --- whos_editing (slice 3) ---

@pytest.mark.parametrize("query,pfile,expected", [
    ("src/auth/jwt.py", "src/auth/jwt.py", True),               # exact
    ("/repo/src/auth/jwt.py", "src/auth/jwt.py", True),          # absolute query vs rel
    ("src/auth/jwt.py", "/repo/src/auth/jwt.py", True),          # rel query vs absolute
    ("src/auth/jwt.py", "src/auth/middleware.py", False),        # different file
    ("auth/jwt.py", "src/auth/jwt.py", True),                    # suffix
    ("", "src/x.py", False),
])
def test_path_matches(query, pfile, expected):
    assert _path_matches(query, pfile) is expected


@pytest.mark.asyncio
async def test_whos_editing_reports_other_editor(client):
    await client.post("/api/presence", json={
        "developer": "alice", "agent": "claude-code",
        "files": ["src/auth/jwt.py"], "intent": "rewriting token validation"})

    # bob checks the same file (excluding himself)
    resp = await client.post("/api/presence/check", json={
        "paths": ["src/auth/jwt.py", "src/db/orm.py"], "exclude_developer": "bob"})
    assert resp.status_code == 200
    by_path = {r["path"]: r for r in resp.json()}
    assert by_path["src/auth/jwt.py"]["editors"][0]["developer"] == "alice"
    assert by_path["src/auth/jwt.py"]["editors"][0]["intent"] == "rewriting token validation"
    assert by_path["src/db/orm.py"]["editors"] == []   # nobody there


@pytest.mark.asyncio
async def test_whos_editing_excludes_self(client):
    await client.post("/api/presence", json={
        "developer": "alice", "agent": "claude-code", "files": ["src/auth/jwt.py"]})
    resp = await client.post("/api/presence/check", json={
        "paths": ["src/auth/jwt.py"], "exclude_developer": "alice"})
    assert resp.json()[0]["editors"] == []   # alice excludes herself -> clear


# --- per-session presence (concurrent same-developer sessions) ---

@pytest.mark.asyncio
async def test_concurrent_same_developer_sessions_coexist(client):
    # Two claude sessions, same git user, different per-session agent labels.
    await client.post("/api/presence", json={
        "developer": "pat", "agent": "claude-code:aaaaaaaa", "files": ["a.py"]})
    await client.post("/api/presence", json={
        "developer": "pat", "agent": "claude-code:bbbbbbbb", "files": ["b.py"]})
    listed = (await client.get("/api/presence")).json()
    # keyed by (developer, agent) -> both survive instead of clobbering
    assert len(listed) == 2
    assert {e["agent"] for e in listed} == {"claude-code:aaaaaaaa", "claude-code:bbbbbbbb"}


@pytest.mark.asyncio
async def test_whos_editing_excludes_only_my_session_not_developer(client):
    # session aaa is editing jwt.py; session bbb (SAME developer) checks it.
    await client.post("/api/presence", json={
        "developer": "pat", "agent": "claude-code:aaaaaaaa",
        "files": ["src/auth/jwt.py"], "intent": "session aaa work"})

    # bbb excludes only itself -> must still SEE aaa (the old developer-keyed code
    # would have hidden it because both are 'pat').
    resp = await client.post("/api/presence/check", json={
        "paths": ["src/auth/jwt.py"],
        "exclude_developer": "pat", "exclude_agent": "claude-code:bbbbbbbb"})
    editors = resp.json()[0]["editors"]
    assert len(editors) == 1 and editors[0]["agent"] == "claude-code:aaaaaaaa"

    # aaa excludes itself -> clear (don't self-report).
    resp = await client.post("/api/presence/check", json={
        "paths": ["src/auth/jwt.py"],
        "exclude_developer": "pat", "exclude_agent": "claude-code:aaaaaaaa"})
    assert resp.json()[0]["editors"] == []


def test_build_presence_agent_is_per_session():
    # session_id from the payload becomes the agent token, matching the MCP's
    # session_agent_label so whos_editing.exclude_agent omits exactly this session.
    body = build_presence(
        {"tool_name": "Edit", "tool_input": {"file_path": "z.py"},
         "session_id": "abcdef12-3456-7890"},
        {"ATS_AGENT": "claude-code"},
    )
    assert body["agent"] == "claude-code:abcdef12"
