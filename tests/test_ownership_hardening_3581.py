"""#3581 review findings (Codex VERIFY BLOCK on 1f0e99b), each against a real
git repository:

1. a pattern is placed by its OWN lock's root before comparing, so an absolute
   pattern under a worktree is the same file as the relative one in the main
   checkout; an absolute pattern outside the session's repo_root is refused.
2. a path through an in-repo symlink is the file it reaches.
3. a session holding live locks cannot be re-anchored.
4. acquisition is serialized: N simultaneous overlapping requests -> one 201.
5. an unreadable operator config refuses anchored acquisitions (fail closed).
Plus: lane restoration applies the pattern validation.
"""
from __future__ import annotations

import asyncio
import json
import os
import socket
import subprocess
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import httpx
import pytest

import ai_team_sync
from ai_team_sync.background_tasks import auto_complete_stale_sessions
from ai_team_sync.config import settings
from ai_team_sync.models import ScopeLock, Session
from ai_team_sync.repo_files import RepoContext
from tests.test_ownership_enforcement_3581 import (
    BASE_FILES, _git, _govern, _lock, _make_repo, _session, _sid)


@pytest.fixture
def repo(tmp_path, monkeypatch):
    root = _make_repo(tmp_path / "repo", BASE_FILES)
    _govern(monkeypatch, tmp_path, root)
    return root


def _long_ago():
    return datetime.now(timezone.utc) - timedelta(hours=settings.session_inactivity_hours + 24)


# ── 1. placement by the lock's own root ──────────────────────────────────

def test_an_absolute_pattern_under_a_worktree_is_the_relative_file(tmp_path):
    main = _make_repo(tmp_path / "main", BASE_FILES)
    worktree = tmp_path / "wt"
    _git(main, "worktree", "add", "-q", "-b", "side", str(worktree))
    wt = str(worktree.resolve())
    ctx = RepoContext()
    assert ctx.place(f"{wt}/frontend/components/X.vue", wt) == (main, "frontend/components/X.vue")
    assert ctx.overlap("frontend/components/X.vue", main,
                       f"{wt}/frontend/components/X.vue", wt)
    assert ctx.overlap(f"{wt}/frontend/components/X.vue", wt,
                       "frontend/components/**", main)
    assert not ctx.overlap("docs/a.md", main, f"{wt}/frontend/components/X.vue", wt)


@pytest.mark.asyncio
async def test_a_worktree_sessions_absolute_lock_blocks_the_main_checkout(
        client, repo, tmp_path):
    worktree = tmp_path / "wt"
    _git(repo, "worktree", "add", "-q", "-b", "side", str(worktree))
    wt = str(worktree.resolve())
    holder = await _sid(client, wt)
    assert (await _lock(client, holder, f"{wt}/frontend/components/X.vue")).status_code == 201

    other = await _sid(client, repo)
    resp = await _lock(client, other, "frontend/components/X.vue")
    assert resp.status_code == 409, resp.text
    assert resp.json()["detail"]["conflicts"][0]["session_id"] == holder


@pytest.mark.asyncio
async def test_a_legacy_absolute_lock_from_another_repo_is_placed_where_it_points(
        client, db_session, repo, tmp_path):
    elsewhere = _make_repo(tmp_path / "other", ("README.md",))
    holder = Session(developer="patrick", agent="claude-code:coordinator", status="active",
                     repo_root=elsewhere, creator_uid=os.getuid(),
                     last_heartbeat=datetime.now(timezone.utc))
    db_session.add(holder)
    await db_session.flush()
    db_session.add(ScopeLock(session_id=holder.id, mode="advisory",
                             pattern=f"{repo}/frontend/components/X.vue"))
    await db_session.commit()

    other = await _sid(client, repo)
    resp = await _lock(client, other, "frontend/components/X.vue")
    assert resp.status_code == 409, resp.text
    assert resp.json()["detail"]["conflicts"][0]["session_id"] == holder.id


@pytest.mark.asyncio
@pytest.mark.parametrize("governed", [True, False])
async def test_an_absolute_pattern_outside_the_repo_root_is_refused(
        client, tmp_path, monkeypatch, governed):
    root = _make_repo(tmp_path / "r", BASE_FILES)
    elsewhere = _make_repo(tmp_path / "elsewhere", ("x.py",))
    if governed:
        _govern(monkeypatch, tmp_path, root)
    outside = f"{elsewhere}/x.py"

    created = await _session(client, root, [outside])
    assert created.status_code == 422, created.text
    assert created.json()["detail"]["error"] == "pattern_outside_repo_root"
    sid = await _sid(client, root)
    resp = await _lock(client, sid, outside)
    assert resp.status_code == 422
    assert resp.json()["detail"]["error"] == "pattern_outside_repo_root"
    # Inside the root, the absolute spelling is accepted.
    assert (await _lock(client, sid, f"{root}/docs/a.md")).status_code == 201
    # An unanchored legacy session has no root to be outside of.
    legacy = await _sid(client, "")
    assert (await _lock(client, legacy, outside)).status_code == 201


@pytest.mark.asyncio
async def test_mcp_extend_scope_reports_an_outside_pattern(client, repo, tmp_path, monkeypatch):
    from tests.test_ownership_enforcement_3581 import _route_mcp_into

    mcp = _route_mcp_into(client, monkeypatch)
    me = await _sid(client, repo, ["docs/a.md"])
    monkeypatch.setattr(mcp, "load_session_id", lambda: me)
    out = (await mcp.call_tool("extend_scope", {"patterns": ["/somewhere/else.py"]}))[0].text
    assert "HTTP 422" in out and "outside this session's repo_root" in out
    assert (await client.get(f"/api/sessions/{me}")).json()["scope"] == ["docs/a.md"]


# ── 2. symlink aliases ───────────────────────────────────────────────────

@pytest.fixture
def linked_repo(tmp_path, monkeypatch):
    root = tmp_path / "linked"
    root.mkdir()
    (root / "src").mkdir()
    (root / "src/a.py").write_text("a\n")
    (root / "src/b.py").write_text("b\n")
    os.symlink("src", root / "lnk")
    os.symlink("src/a.py", root / "alias.py")
    _git(root, "init", "-q", "-b", "main")
    _git(root, "add", "-A")
    _git(root, "commit", "-q", "-m", "init")
    resolved = str(root.resolve())
    _govern(monkeypatch, tmp_path, resolved)
    return resolved


def test_the_index_maps_aliases_to_real_files(linked_repo):
    index = RepoContext().index(linked_repo)
    assert index.names["lnk/a.py"] == "src/a.py"
    assert index.names["alias.py"] == "src/a.py"
    assert {"src/a.py", "lnk/a.py", "alias.py"} <= index.names_of("src/a.py")
    assert index.resolve("lnk/new.py") == "src/new.py"


@pytest.mark.asyncio
@pytest.mark.parametrize("alias", ["lnk/a.py", "lnk/**", "alias.py", "lnk/*.py"])
async def test_a_symlink_alias_is_the_same_file(client, linked_repo, alias):
    holder = await _sid(client, linked_repo, ["src/a.py"])
    other = await _sid(client, linked_repo)
    resp = await _lock(client, other, alias)
    assert resp.status_code == 409, resp.text
    assert resp.json()["detail"]["conflicts"][0]["session_id"] == holder


@pytest.mark.asyncio
async def test_a_new_file_through_a_symlinked_directory_is_the_real_one(client, linked_repo):
    await _sid(client, linked_repo, ["src/new.py"])
    other = await _sid(client, linked_repo)
    assert (await _lock(client, other, "lnk/new.py")).status_code == 409
    assert (await _lock(client, other, "src/b.py")).status_code == 201


@pytest.mark.asyncio
async def test_the_ownership_report_follows_symlinks(client, linked_repo):
    a = await _sid(client, linked_repo, ["src/a.py"])
    b = await _sid(client, linked_repo, ["lnk/b.py"])
    body = (await client.get("/api/locks/ownership", params={
        "repo_root": linked_repo, "paths": "lnk/a.py,alias.py,src/b.py",
        "session_id": a})).json()
    by_path = {r["path"]: r for r in body["paths"]}
    assert [h["session_id"] for h in by_path["lnk/a.py"]["holders"]] == [a]
    assert by_path["lnk/a.py"]["real"] == "src/a.py"
    assert [h["session_id"] for h in by_path["alias.py"]["holders"]] == [a]
    assert [h["session_id"] for h in by_path["src/b.py"]["holders"]] == [b]
    assert by_path["src/b.py"]["conflict"] is True


# ── 3. re-anchoring a session that holds live locks ──────────────────────

@pytest.mark.asyncio
async def test_a_session_holding_live_locks_cannot_be_reanchored(client, repo, tmp_path):
    other_repo = _make_repo(tmp_path / "other", BASE_FILES)
    sid = await _sid(client, repo, ["docs/a.md"])
    token = {"X-ATS-Approval-Token": client.tokens[sid]}

    moved = await client.patch(f"/api/sessions/{sid}",
                               json={"repo_root": other_repo, "summary": "moved"}, headers=token)
    assert moved.status_code == 409, moved.text
    assert moved.json()["detail"]["error"] == "repo_root_change_with_live_locks"
    assert moved.json()["detail"]["locks"] == ["docs/a.md"]
    row = (await client.get(f"/api/sessions/{sid}")).json()
    assert row["repo_root"] == repo and (row["summary"] or "") != "moved"

    assert (await client.patch(f"/api/sessions/{sid}", json={"repo_root": repo + "/"},
                               headers=token)).status_code == 200
    unlocked = await _sid(client, repo)
    assert (await client.patch(f"/api/sessions/{unlocked}", json={"repo_root": other_repo},
                               headers={"X-ATS-Approval-Token": client.tokens[unlocked]})
            ).status_code == 200


# ── 4. serialized acquisition ────────────────────────────────────────────

@pytest.mark.asyncio
async def test_simultaneous_overlapping_locks_grant_exactly_one(client, repo):
    sids = [await _sid(client, repo) for _ in range(8)]
    patterns = ["frontend/components/X.vue", "frontend/components/**", "frontend/*/X.vue",
                "frontend/**/X.vue"]
    results = await asyncio.gather(*[
        _lock(client, sid, patterns[i % len(patterns)]) for i, sid in enumerate(sids)])
    codes = sorted(r.status_code for r in results)
    assert codes == [201] + [409] * 7, [r.text for r in results]


@pytest.mark.asyncio
async def test_simultaneous_overlapping_sessions_grant_exactly_one(client, repo):
    results = await asyncio.gather(*[
        _session(client, repo, ["frontend/components/X.vue"], agent=f"claude-code:{i:08d}")
        for i in range(6)])
    assert sorted(r.status_code for r in results) == [201] + [409] * 5


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _serve(port: int, env: dict, cwd: Path) -> subprocess.Popen:
    return subprocess.Popen(
        [sys.executable, "-m", "uvicorn", "ai_team_sync.server:app", "--host", "127.0.0.1",
         "--port", str(port), "--no-proxy-headers", "--log-level", "warning"],
        env=env, cwd=cwd, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)


def _wait_healthy(port: int, proc: subprocess.Popen) -> None:
    deadline = time.time() + 30
    while time.time() < deadline:
        if proc.poll() is not None:
            raise AssertionError(proc.stderr.read().decode()[-2000:])
        try:
            if httpx.get(f"http://127.0.0.1:{port}/health", timeout=1).status_code == 200:
                return
        except httpx.HTTPError:
            time.sleep(0.2)
    raise AssertionError("server did not become healthy")


def test_separate_server_processes_grant_exactly_one(tmp_path):
    """Three uvicorn processes over one SQLite file: the in-process lock cannot
    help here, only the database writer lock can."""
    root = _make_repo(tmp_path / "repo", BASE_FILES)
    config = tmp_path / "operator.toml"
    config.write_text(f'[governance]\nrepositories = ["{root}"]\n')
    db_file = tmp_path / "ats.db"
    env = {**os.environ,
           "DATABASE_URL": f"sqlite+aiosqlite:///{db_file}",
           "ATS_OPERATOR_CONFIG": str(config),
           "PYTHONPATH": str(Path(ai_team_sync.__file__).resolve().parent.parent)}
    env.pop("ATS_COORDINATED_REPOS", None)
    ports = [_free_port() for _ in range(3)]
    procs = []
    try:
        procs.append(_serve(ports[0], env, tmp_path))
        _wait_healthy(ports[0], procs[0])  # creates the schema once
        for port in ports[1:]:
            procs.append(_serve(port, env, tmp_path))
        for port, proc in zip(ports[1:], procs[1:]):
            _wait_healthy(port, proc)

        sids = []
        for i in range(9):
            r = httpx.post(f"http://127.0.0.1:{ports[0]}/api/sessions", json={
                "developer": "patrick", "agent": f"claude-code:{i:08d}", "scope": [],
                "auto_lock": False, "repo_root": root})
            assert r.status_code == 201, r.text
            sids.append(r.json()["id"])

        async def fire():
            async with httpx.AsyncClient(timeout=30) as c:
                return await asyncio.gather(*[
                    c.post(f"http://127.0.0.1:{ports[i % 3]}/api/locks", json={
                        "session_id": sid, "pattern": "frontend/components/X.vue",
                        "mode": "advisory"})
                    for i, sid in enumerate(sids)])

        results = asyncio.run(fire())
        assert sorted(r.status_code for r in results) == [201] + [409] * 8, \
            [r.text for r in results]
    finally:
        for proc in procs:
            proc.terminate()
        for proc in procs:
            proc.wait(timeout=10)


# ── 5. an unreadable operator config fails closed ────────────────────────

@pytest.mark.asyncio
async def test_an_unreadable_operator_config_refuses_anchored_acquisitions(
        client, tmp_path, monkeypatch):
    root = _make_repo(tmp_path / "r", BASE_FILES)
    anchored = await _sid(client, root)
    legacy = await _sid(client, "")
    bad = tmp_path / "bad.toml"
    bad.write_text("[governance\nrepositories = oops\n")
    monkeypatch.setenv("ATS_OPERATOR_CONFIG", str(bad))

    created = await _session(client, root, ["docs/a.md"])
    assert created.status_code == 503, created.text
    assert created.json()["detail"]["error"] == "operator_config_unreadable"
    resp = await _lock(client, anchored, "docs/a.md")
    assert resp.status_code == 503
    assert resp.json()["detail"]["error"] == "operator_config_unreadable"

    assert (await _lock(client, legacy, "docs/a.md")).status_code == 201
    assert (await _session(client, "", ["docs/b.md"])).status_code == 201
    assert (await _session(client, root)).status_code == 201  # no acquisition


# ── restoration applies the same validation ──────────────────────────────

@pytest.mark.asyncio
async def test_restoration_does_not_regrant_an_unbacked_lane(client, db_session, repo):
    ghost = Session(developer="patrick", agent="claude-code:ghost", status="active",
                    started_at=_long_ago(), last_heartbeat=_long_ago(), repo_root=repo,
                    creator_uid=os.getuid(),
                    scope=json.dumps(["docs/a.md", "frontend/src/**/Gone.vue"]))
    db_session.add(ghost)
    await db_session.flush()
    for pattern in ("docs/a.md", "frontend/src/**/Gone.vue"):
        db_session.add(ScopeLock(session_id=ghost.id, pattern=pattern, mode="advisory",
                                 created_at=_long_ago(),
                                 expires_at=datetime.now(timezone.utc) + timedelta(hours=4)))
    await db_session.commit()
    assert await auto_complete_stale_sessions(db_session) == 1

    revived = await client.post(f"/api/sessions/{ghost.id}/heartbeat")
    assert revived.status_code == 200, revived.text
    assert revived.json()["locks_restored"] == ["docs/a.md"]
    assert revived.json()["locks_not_restored"] == ["frontend/src/**/Gone.vue"]
    assert revived.json()["not_restored_reasons"] == ["pattern_matches_no_file"]
