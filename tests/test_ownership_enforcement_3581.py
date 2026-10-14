"""#3581: ATS refuses conflicting live ownership in governed repositories.

Reproduced 2026-10-13 in /opt/anime-studio: one session held advisory
'frontend/**', another took advisory 'frontend/components/OperatorPromptEditor.vue',
and both edited the file. A third held exclusive
'frontend/src/**/OperatorPromptEditor.vue', which matched no file at all because
there is no frontend/src. Pattern overlap was fnmatch(a, b) or fnmatch(b, a),
which also called 'frontend/*/X.vue' and 'frontend/src/**' disjoint.

Every test here runs against a real git repository; nothing mocks the matcher.
"""
from __future__ import annotations

import json
import os
import subprocess
from datetime import datetime, timedelta, timezone
from fnmatch import fnmatch

import pytest

from ai_team_sync.background_tasks import auto_complete_stale_sessions
from ai_team_sync.config import settings
from ai_team_sync.models import ScopeLock, Session
from ai_team_sync.repo_files import RepoContext
from ai_team_sync.scope_paths import matching_files, patterns_overlap

BASE_FILES = (
    "README.md",
    "frontend/components/X.vue",
    "frontend/components/OperatorPromptEditor.vue",
    "frontend/api/operatorPrompt.ts",
    "frontend/lib/mapping.ts",
    "docs/a.md",
)


def _git(cwd, *args):
    subprocess.run(["git", "-C", str(cwd), "-c", "user.email=t@example.invalid",
                    "-c", "user.name=t", *args], check=True, capture_output=True)


def _make_repo(path, files):
    path.mkdir(parents=True)
    _git(path, "init", "-q", "-b", "main")
    for rel in files:
        target = path / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(rel + "\n")
    _git(path, "add", "-A")
    _git(path, "commit", "-q", "-m", "init")
    return str(path.resolve())


def _govern(monkeypatch, tmp_path, *roots):
    config = tmp_path / "operator.toml"
    listed = ", ".join(f'"{r}"' for r in roots)
    config.write_text(f"[governance]\nrepositories = [{listed}]\n")
    monkeypatch.setenv("ATS_OPERATOR_CONFIG", str(config))


@pytest.fixture
def repo(tmp_path, monkeypatch):
    root = _make_repo(tmp_path / "repo", BASE_FILES)
    _govern(monkeypatch, tmp_path, root)
    return root


async def _session(client, root, scope=(), mode="advisory", agent="default"):
    resp = await client.post("/api/sessions", json={
        "developer": "patrick", "agent": agent, "scope": list(scope),
        "auto_lock": bool(scope), "lock_mode": mode, "repo_root": root})
    return resp


async def _sid(client, root, scope=(), mode="advisory", agent="default"):
    resp = await _session(client, root, scope, mode, agent)
    assert resp.status_code == 201, resp.text
    if not hasattr(client, "tokens"):
        client.tokens = {}
    client.tokens[resp.json()["id"]] = resp.headers["X-ATS-Approval-Token"]
    return resp.json()["id"]


async def _lock(client, sid, pattern, mode="advisory"):
    return await client.post("/api/locks", json={
        "session_id": sid, "pattern": pattern, "mode": mode})


# ── live ownership conflicts ─────────────────────────────────────────────

@pytest.mark.asyncio
async def test_an_advisory_subtree_holder_blocks_another_sessions_file_claim(client, repo):
    holder = await _sid(client, repo, ["frontend/**"], agent="claude-code:holder01")

    created = await _session(client, repo, ["frontend/components/X.vue"])
    assert created.status_code == 409, created.text
    detail = created.json()["detail"]
    assert detail["error"] == "scope_conflict"
    assert detail["conflicts"][0]["session_id"] == holder
    assert detail["conflicts"][0]["existing_pattern"] == "frontend/**"
    assert detail["conflicts"][0]["existing_agent"] == "claude-code:holder01"
    assert holder in detail["message"] and "claude-code:holder01" in detail["message"]
    assert "governed repo" in detail["message"]

    other = await _sid(client, repo)
    resp = await _lock(client, other, "frontend/components/X.vue")
    assert resp.status_code == 409, resp.text
    assert resp.json()["detail"]["conflicts"][0]["session_id"] == holder


@pytest.mark.asyncio
async def test_an_advisory_claim_over_an_exclusive_holder_is_refused(client, repo):
    await _sid(client, repo, ["frontend/components/**"], mode="exclusive")
    other = await _sid(client, repo)
    resp = await _lock(client, other, "frontend/components/X.vue", "advisory")
    assert resp.status_code == 409
    assert resp.json()["detail"]["conflicts"][0]["lock_mode"] == "exclusive"


@pytest.mark.asyncio
async def test_a_disjoint_claim_in_a_governed_repo_is_allowed(client, repo):
    await _sid(client, repo, ["frontend/components/**"])
    other = await _sid(client, repo)
    assert (await _lock(client, other, "frontend/api/operatorPrompt.ts")).status_code == 201


@pytest.mark.asyncio
async def test_star_segment_and_subtree_overlap_when_a_file_matches_both(
        client, tmp_path, monkeypatch):
    root = _make_repo(tmp_path / "r", BASE_FILES + ("frontend/src/X.vue",))
    _govern(monkeypatch, tmp_path, root)
    # The legacy two-way fnmatch calls these disjoint.
    assert not fnmatch("frontend/*/X.vue", "frontend/src/**")
    assert not fnmatch("frontend/src/**", "frontend/*/X.vue")

    holder = await _sid(client, root, ["frontend/src/**"])
    other = await _sid(client, root)
    resp = await _lock(client, other, "frontend/*/X.vue")
    assert resp.status_code == 409, resp.text
    assert resp.json()["detail"]["conflicts"][0]["session_id"] == holder


@pytest.mark.asyncio
async def test_star_segment_and_subtree_do_not_overlap_when_no_file_matches_both(
        client, tmp_path, monkeypatch):
    root = _make_repo(tmp_path / "r", BASE_FILES + ("frontend/src/other.ts",))
    _govern(monkeypatch, tmp_path, root)

    await _sid(client, root, ["frontend/src/**"])
    other = await _sid(client, root)
    assert (await _lock(client, other, "frontend/*/X.vue")).status_code == 201


@pytest.mark.asyncio
async def test_a_session_may_overlap_its_own_locks(client, repo):
    me = await _sid(client, repo, ["frontend/**"])
    assert (await _lock(client, me, "frontend/components/X.vue", "exclusive")).status_code == 201
    assert (await _lock(client, me, "frontend/*/X.vue")).status_code == 201


@pytest.mark.asyncio
async def test_an_approved_override_lets_the_requester_through(client, repo):
    owner = await _sid(client, repo, ["frontend/**"])
    requester = await _sid(client, repo)
    assert (await _lock(client, requester, "frontend/components/X.vue")).status_code == 409

    req = await client.post("/api/override-requests", json={
        "requester_session_id": requester, "conflicting_pattern": "frontend/**",
        "justification": "one file"})
    assert req.status_code == 201, req.text
    assert req.json()["status"] == "pending"
    approved = await client.post(
        f"/api/override-requests/{req.json()['id']}/respond",
        json={"approved": True, "actor_session_id": owner},
        headers={"X-ATS-Approval-Token": client.tokens[owner]})
    assert approved.status_code == 200, approved.text

    assert (await _lock(client, requester, "frontend/components/X.vue")).status_code == 201


@pytest.mark.asyncio
async def test_a_completed_holder_no_longer_blocks(client, repo):
    holder = await _sid(client, repo, ["frontend/**"])
    other = await _sid(client, repo)
    assert (await _lock(client, other, "frontend/components/X.vue")).status_code == 409
    done = await client.patch(f"/api/sessions/{holder}", json={"status": "completed"},
                              headers={"X-ATS-Approval-Token": client.tokens[holder]})
    assert done.status_code == 200, done.text
    assert (await _lock(client, other, "frontend/components/X.vue")).status_code == 201


@pytest.mark.asyncio
async def test_a_reaped_holder_no_longer_blocks(client, db_session, repo):
    long_ago = datetime.now(timezone.utc) - timedelta(hours=settings.session_inactivity_hours + 24)
    ghost = Session(developer="patrick", agent="claude-code:ghost", status="active",
                    started_at=long_ago, last_heartbeat=long_ago, repo_root=repo)
    db_session.add(ghost)
    await db_session.flush()
    db_session.add(ScopeLock(session_id=ghost.id, pattern="frontend/**", mode="advisory",
                             created_at=long_ago,
                             expires_at=datetime.now(timezone.utc) + timedelta(hours=8)))
    await db_session.commit()

    other = await _sid(client, repo)
    assert (await _lock(client, other, "frontend/components/X.vue")).status_code == 409
    assert await auto_complete_stale_sessions(db_session) >= 1
    assert (await _lock(client, other, "frontend/components/X.vue")).status_code == 201


@pytest.mark.asyncio
async def test_restoration_does_not_regrant_a_lane_another_session_now_holds(
        client, db_session, repo):
    long_ago = datetime.now(timezone.utc) - timedelta(hours=settings.session_inactivity_hours + 24)
    ghost = Session(developer="patrick", agent="claude-code:ghost", status="active",
                    started_at=long_ago, last_heartbeat=long_ago, repo_root=repo,
                    creator_uid=os.getuid(), scope=json.dumps(["frontend/**"]))
    db_session.add(ghost)
    await db_session.flush()
    db_session.add(ScopeLock(session_id=ghost.id, pattern="frontend/**", mode="advisory",
                             created_at=long_ago,
                             expires_at=datetime.now(timezone.utc) + timedelta(hours=4)))
    await db_session.commit()
    assert await auto_complete_stale_sessions(db_session) == 1

    newcomer = await _sid(client, repo)
    assert (await _lock(client, newcomer, "frontend/components/X.vue")).status_code == 201

    revived = await client.post(f"/api/sessions/{ghost.id}/heartbeat")
    assert revived.status_code == 200, revived.text
    assert revived.json()["locks_not_restored"] == ["frontend/**"]
    assert revived.json()["not_restored_reasons"] == ["newer_holder"]


@pytest.mark.asyncio
async def test_a_worktree_anchored_session_is_the_same_repository(client, repo, tmp_path):
    worktree = tmp_path / "wt"
    _git(repo, "worktree", "add", "-q", "-b", "side", str(worktree))
    (worktree / "frontend/components/New.vue").write_text("new\n")

    holder = await _sid(client, repo, ["frontend/components/**"])
    other = await _sid(client, str(worktree.resolve()))
    resp = await _lock(client, other, "frontend/components/X.vue")
    assert resp.status_code == 409, resp.text
    assert resp.json()["detail"]["conflicts"][0]["session_id"] == holder
    # An absolute pattern under the worktree root names the repo-relative file.
    absolute = f"{worktree.resolve()}/frontend/api/operatorPrompt.ts"
    assert (await _lock(client, other, absolute)).status_code == 201
    # A file that so far exists only on the worktree still backs a glob claim:
    # the refusal is the held subtree, not "matches no file".
    third = await _sid(client, repo)
    resp = await _lock(client, third, "frontend/*/New.v?e")
    assert resp.status_code == 409, resp.text
    assert resp.json()["detail"]["error"] == "scope_conflict"
    assert (await _lock(client, third, "docs/New.v?e")).status_code == 422


# ── patterns must name real files ────────────────────────────────────────

@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["advisory", "exclusive"])
async def test_a_glob_matching_no_file_is_refused(client, repo, mode):
    pattern = "frontend/src/**/OperatorPromptEditor.vue"
    created = await _session(client, repo, [pattern], mode=mode)
    assert created.status_code == 422, created.text
    detail = created.json()["detail"]
    assert detail["error"] == "pattern_matches_no_file"
    assert detail["pattern"] == pattern and detail["repo_root"] == repo
    assert repo in detail["message"] and pattern in detail["message"]
    assert "nearest existing directory: 'frontend'" in detail["message"]
    assert "frontend/components/OperatorPromptEditor.vue" in detail["message"]

    sid = await _sid(client, repo)
    resp = await _lock(client, sid, pattern, mode)
    assert resp.status_code == 422
    assert resp.json()["detail"]["error"] == "pattern_matches_no_file"


@pytest.mark.asyncio
async def test_a_new_file_in_an_existing_directory_is_allowed(client, repo):
    sid = await _sid(client, repo, ["frontend/components/Brand.vue"], mode="exclusive")
    assert (await _lock(client, sid, "NEW_TOP_LEVEL.md")).status_code == 201


@pytest.mark.asyncio
async def test_a_new_file_in_a_missing_directory_is_refused(client, repo):
    created = await _session(client, repo, ["frontend/src/components/Brand.vue"])
    assert created.status_code == 422
    detail = created.json()["detail"]
    assert detail["error"] == "pattern_parent_missing"
    assert "nearest existing directory: 'frontend'" in detail["message"]


@pytest.mark.asyncio
async def test_a_bare_directory_is_refused_with_the_subtree_form(client, repo):
    sid = await _sid(client, repo)
    resp = await _lock(client, sid, "frontend/components")
    assert resp.status_code == 422
    assert "frontend/components/**" in resp.json()["detail"]["message"]


@pytest.mark.asyncio
async def test_a_claim_broader_than_the_limit_is_refused(client, repo, monkeypatch):
    monkeypatch.setenv("ATS_BROAD_CLAIM_LIMIT", "3")
    created = await _session(client, repo, ["frontend/**"])
    assert created.status_code == 422
    detail = created.json()["detail"]
    assert detail["error"] == "pattern_too_broad"
    assert "4 files" in detail["message"] and "ATS_BROAD_CLAIM_LIMIT" in detail["message"]
    assert (await _session(client, repo, ["**"])).status_code == 422
    assert (await _session(client, repo, ["frontend/components/**"])).status_code == 201


@pytest.mark.asyncio
async def test_existing_locks_are_not_revoked_when_governance_starts(
        client, tmp_path, monkeypatch):
    root = _make_repo(tmp_path / "r", BASE_FILES)
    holder = await _sid(client, root, ["frontend/src/**/Gone.vue"], mode="exclusive")
    _govern(monkeypatch, tmp_path, root)

    held = [lk["pattern"] for lk in (await client.get("/api/locks")).json()
            if lk["session_id"] == holder]
    assert held == ["frontend/src/**/Gone.vue"]
    assert (await _lock(client, holder, "frontend/src/**/Other.vue")).status_code == 422


# ── non-governed repositories keep the old rule ──────────────────────────

@pytest.mark.asyncio
async def test_a_non_governed_repo_keeps_shared_advisory_and_unbacked_patterns(
        client, tmp_path):
    root = _make_repo(tmp_path / "r", BASE_FILES)
    await _sid(client, root, ["frontend/**"])
    other = await _sid(client, root)
    assert (await _lock(client, other, "frontend/components/X.vue")).status_code == 201
    assert (await _lock(client, other, "nowhere/**")).status_code == 201
    assert (await _lock(client, other, "frontend/components/X.vue", "exclusive")).status_code == 409


# ── ownership report ─────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_ownership_report_names_holders_and_conflicts(client, repo, tmp_path):
    a = await _sid(client, repo, ["frontend/**"], agent="claude-code:aaaa")
    b = await _sid(client, repo, ["docs/a.md"], mode="exclusive", agent="codex:bbbb")
    elsewhere = _make_repo(tmp_path / "other", BASE_FILES)
    await _sid(client, elsewhere, ["README.md"])

    resp = await client.get("/api/locks/ownership", params={
        "repo_root": repo, "session_id": a,
        "paths": f"frontend/components/X.vue,docs/a.md,README.md,{repo}/frontend/api/operatorPrompt.ts"})
    assert resp.status_code == 200, resp.text
    body = resp.json()
    by_path = {r["path"]: r for r in body["paths"]}

    x = by_path["frontend/components/X.vue"]
    assert [(h["session_id"], h["mode"], h["pattern"]) for h in x["holders"]] == \
        [(a, "advisory", "frontend/**")]
    assert x["conflict"] is False
    doc = by_path["docs/a.md"]
    assert [(h["session_id"], h["mode"]) for h in doc["holders"]] == [(b, "exclusive")]
    assert doc["conflict"] is True
    assert by_path["README.md"]["holders"] == []  # held only in another repository
    absolute = by_path[f"{repo}/frontend/api/operatorPrompt.ts"]
    assert absolute["relative"] == "frontend/api/operatorPrompt.ts"
    assert [h["session_id"] for h in absolute["holders"]] == [a]
    assert body["conflict"] is True

    anonymous = (await client.get("/api/locks/ownership", params={
        "repo_root": repo, "paths": "frontend/components/X.vue"})).json()
    assert anonymous["paths"][0]["conflict"] is True
    assert (await client.get("/api/locks/ownership", params={"repo_root": repo})).status_code == 422


# ── MCP: extend_scope goes through POST /api/locks; lock_ownership wraps the report

def _route_mcp_into(app_client, monkeypatch):
    import ai_team_sync.mcp.server as mcp
    from httpx import AsyncClient

    class _ASGIClient:
        def __init__(self, *a, **k):
            self._c = AsyncClient(transport=app_client._transport,
                                  base_url="http://localhost:8400")

        async def __aenter__(self):
            return self._c

        async def __aexit__(self, *exc):
            await self._c.aclose()

    monkeypatch.setattr(mcp.httpx, "AsyncClient", _ASGIClient)
    return mcp


@pytest.mark.asyncio
async def test_mcp_extend_scope_and_lock_ownership(client, repo, monkeypatch):
    mcp = _route_mcp_into(client, monkeypatch)
    holder = await _sid(client, repo, ["frontend/**"], agent="claude-code:holder01")
    me = await _sid(client, repo, ["docs/a.md"], agent="claude-code:me000001")
    monkeypatch.setattr(mcp, "load_session_id", lambda: me)

    out = (await mcp.call_tool("extend_scope", {
        "patterns": ["frontend/components/X.vue", "frontend/src/**/Nope.vue"]}))[0].text
    assert "frontend/components/X.vue" in out and holder in out
    assert "HTTP 422" in out and "matches no file" in out
    scope = (await client.get(f"/api/sessions/{me}")).json()["scope"]
    assert scope == ["docs/a.md"]

    report = (await mcp.call_tool("lock_ownership", {
        "paths": ["frontend/components/X.vue", "docs/a.md"], "repo_root": repo}))[0].text
    assert "frontend/components/X.vue [CONFLICT]" in report
    assert "docs/a.md [ok]" in report and "(YOUR SESSION)" in report


# ── the overlap function itself ──────────────────────────────────────────

def test_overlap_on_a_real_file_set(tmp_path):
    root = _make_repo(tmp_path / "r", BASE_FILES + ("frontend/src/other.ts",))
    files = RepoContext().files(root)
    assert "frontend/components/X.vue" in files
    assert patterns_overlap("frontend/**", "frontend/components/X.vue", files, root)
    assert patterns_overlap("**", "docs/a.md", files, root)
    assert patterns_overlap("**/*.vue", "frontend/components/**", files, root)
    assert not patterns_overlap("frontend/*/X.vue", "frontend/src/**", files, root)
    assert not patterns_overlap("docs/**", "frontend/**", files, root)
    # A literal file not created yet is covered by the subtree it lands in.
    assert patterns_overlap("frontend/components/New.vue", "frontend/components/**", files, root)
    assert patterns_overlap("frontend/components/New.vue", "frontend/components/New.vue",
                            files, root)
    assert matching_files("frontend/*", files, root) >= {"frontend/components/X.vue"}


@pytest.mark.parametrize("a,b,expected", [
    ("frontend/*/X.vue", "frontend/src/**", True),      # uncertain without files
    ("frontend/**", "frontend/components/X.vue", True),
    ("**", "anything/at/all.py", True),
    ("src/a/**", "src/b/**", False),
    ("src/auth/**", "src/models/**", False),
    ("src", "src/x.py", True),                          # a directory covers its files
    ("*.py", "docs/a.md", True),                        # no literal prefix: uncertain
    ("docs/a.md", "docs/b.md", False),
])
def test_overlap_without_a_file_set_is_conservative(a, b, expected):
    assert patterns_overlap(a, b, None, "/srv/not-a-repo") is expected
    assert patterns_overlap(b, a, None, "/srv/not-a-repo") is expected
