"""check_git_changes must answer about the SESSION's repo, and actually render.

Two independent defects made this tool a permanent false all-clear, found
2026-08-25 while driving it as the coordination surface:

  1. routers/git_status.py called get_repo_root() with no argument, which
     resolves from Path.cwd() -- the ATS SERVER's cwd, not the session's.
     session.repo_root was loaded two lines earlier and never read. Every
     session working in any other repo was answered about the wrong tree.

  2. mcp/server.py parsed files_in_scope / files_out_of_scope. The endpoint
     returns uncommitted_files / files_by_pattern / total_files. Neither key
     has ever existed, so both lists were always [] and the tool returned
     "No uncommitted changes" unconditionally -- regardless of defect 1.

Defect 2 alone means the tool could never report anything, so defect 1 was
invisible behind it. Both are pinned here; either one regressing fails.

This is the SAME wiring mismatch already fixed once for pre_commit_check
(tests/test_mcp_pre_commit_check.py: "the response never matched -- it ALWAYS
said clear") and never swept for the sibling tool.
"""
from __future__ import annotations

import subprocess
from pathlib import Path

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

import ai_team_sync.mcp.server as mcp
from ai_team_sync.database import get_db
from ai_team_sync.server import create_app

DIRTY = "packages/app/thing.py"
CLEAN_PATTERN = "packages/other/**"


def _git(repo: Path, *args: str) -> None:
    subprocess.run(
        ["git", "-c", "user.email=t@t", "-c", "user.name=t", *args],
        cwd=repo, check=True, capture_output=True,
    )


def _repo_with_dirty_file(root: Path) -> Path:
    """A real git repo whose only uncommitted change is DIRTY."""
    root.mkdir(parents=True, exist_ok=True)
    _git(root, "init", "-q")
    target = root / DIRTY
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("original\n")
    _git(root, "add", "-A")
    _git(root, "commit", "-qm", "base")
    target.write_text("modified\n")  # now uncommitted
    return root


def _clean_repo(root: Path) -> Path:
    root.mkdir(parents=True, exist_ok=True)
    _git(root, "init", "-q")
    (root / "README").write_text("x\n")
    _git(root, "add", "-A")
    _git(root, "commit", "-qm", "base")
    return root


def _wire(monkeypatch, db_engine):
    """Route the MCP client's httpx at the in-process ASGI app."""
    app = create_app()
    factory = async_sessionmaker(db_engine, class_=AsyncSession, expire_on_commit=False)

    async def override_get_db():
        async with factory() as s:
            yield s

    app.dependency_overrides[get_db] = override_get_db
    transport = ASGITransport(app=app)

    class _ASGIClient:
        def __init__(self, *a, **k):
            self._c = AsyncClient(transport=transport, base_url="http://localhost:8400")

        async def __aenter__(self):
            return self._c

        async def __aexit__(self, *exc):
            await self._c.aclose()

    monkeypatch.setattr(mcp.httpx, "AsyncClient", _ASGIClient)
    return transport


async def _make_session(c: AsyncClient, *, repo_root: str, scope: list[str]) -> str:
    r = await c.post("/api/sessions", json={
        "developer": "tester", "agent": "claude-code",
        "scope": scope, "repo_root": repo_root, "auto_lock": False})
    r.raise_for_status()
    return r.json()["id"]


# ── defect 1: the endpoint must inspect the session's repo ──────────────────

@pytest.mark.asyncio
async def test_endpoint_reports_the_dirty_file_in_the_sessions_repo(
        db_engine, monkeypatch, tmp_path):
    transport = _wire(monkeypatch, db_engine)
    repo = _repo_with_dirty_file(tmp_path / "sessionrepo")

    # The server's cwd is somewhere else entirely, and is CLEAN -- so a passing
    # result here cannot come from the old cwd path.
    monkeypatch.chdir(_clean_repo(tmp_path / "servercwd"))

    async with AsyncClient(transport=transport, base_url="http://localhost:8400") as c:
        sid = await _make_session(c, repo_root=str(repo), scope=[DIRTY])
        data = (await c.get(f"/api/git/session/{sid}/changes")).json()

    assert data["uncommitted_files"] == [DIRTY], (
        "the endpoint did not inspect the session's repo_root")
    assert data["total_files"] == 1


@pytest.mark.asyncio
async def test_a_dirty_repo_that_is_not_the_sessions_is_not_reported(
        db_engine, monkeypatch, tmp_path):
    """The inverse of the false all-clear: it must not report the WRONG repo's
    dirt either. Server cwd is dirty; the session's own repo is clean."""
    transport = _wire(monkeypatch, db_engine)
    clean = _clean_repo(tmp_path / "sessionrepo")
    monkeypatch.chdir(_repo_with_dirty_file(tmp_path / "servercwd"))

    async with AsyncClient(transport=transport, base_url="http://localhost:8400") as c:
        sid = await _make_session(c, repo_root=str(clean), scope=[DIRTY])
        data = (await c.get(f"/api/git/session/{sid}/changes")).json()

    assert data["uncommitted_files"] == [], (
        "the endpoint reported the SERVER's dirty repo as the session's work")


@pytest.mark.asyncio
async def test_dirty_file_outside_the_declared_scope_is_not_reported(
        db_engine, monkeypatch, tmp_path):
    transport = _wire(monkeypatch, db_engine)
    repo = _repo_with_dirty_file(tmp_path / "sessionrepo")
    monkeypatch.chdir(_clean_repo(tmp_path / "servercwd"))

    async with AsyncClient(transport=transport, base_url="http://localhost:8400") as c:
        sid = await _make_session(c, repo_root=str(repo), scope=[CLEAN_PATTERN])
        data = (await c.get(f"/api/git/session/{sid}/changes")).json()

    assert data["uncommitted_files"] == []


@pytest.mark.asyncio
async def test_unanchored_session_keeps_the_legacy_cwd_behaviour(
        db_engine, monkeypatch, tmp_path):
    """A session with no repo_root predates #2554. Falling back to cwd is
    deliberate -- returning silence to a caller that may well be in the right
    repo would be a regression, not a fix."""
    transport = _wire(monkeypatch, db_engine)
    monkeypatch.chdir(_repo_with_dirty_file(tmp_path / "servercwd"))

    async with AsyncClient(transport=transport, base_url="http://localhost:8400") as c:
        sid = await _make_session(c, repo_root="", scope=[DIRTY])
        data = (await c.get(f"/api/git/session/{sid}/changes")).json()

    assert data["uncommitted_files"] == [DIRTY]


# ── defect 2: the MCP tool must render what the endpoint sends ──────────────

@pytest.mark.asyncio
async def test_mcp_tool_names_the_file_instead_of_saying_no_changes(
        db_engine, monkeypatch, tmp_path):
    """The regression that matters most: before the fix this returned
    '✅ No uncommitted changes.' for every possible input, because it read two
    keys the endpoint has never sent."""
    transport = _wire(monkeypatch, db_engine)
    repo = _repo_with_dirty_file(tmp_path / "sessionrepo")
    monkeypatch.chdir(_clean_repo(tmp_path / "servercwd"))

    # Stay inside the client block: the in-memory SQLite database is dropped
    # once its last connection closes.
    async with AsyncClient(transport=transport, base_url="http://localhost:8400") as c:
        sid = await _make_session(c, repo_root=str(repo), scope=[DIRTY])
        # call_tool reads the id via load_session_id() into a LOCAL, so the
        # module attribute is not the seam — patch the function.
        monkeypatch.setattr(mcp, "load_session_id", lambda: sid)
        out = await mcp.call_tool("check_git_changes", {})

    text = out[0].text
    assert "No uncommitted changes" not in text, (
        "the tool still reports a false all-clear over a real dirty file")
    assert DIRTY in text, f"the tool did not name the dirty file; got: {text!r}"


@pytest.mark.asyncio
async def test_mcp_tool_says_clear_only_when_the_repo_really_is_clean(
        db_engine, monkeypatch, tmp_path):
    """The all-clear must still be reachable -- otherwise the test above could
    be satisfied by a tool that never says it."""
    transport = _wire(monkeypatch, db_engine)
    clean = _clean_repo(tmp_path / "sessionrepo")
    monkeypatch.chdir(_clean_repo(tmp_path / "servercwd"))

    async with AsyncClient(transport=transport, base_url="http://localhost:8400") as c:
        sid = await _make_session(c, repo_root=str(clean), scope=[DIRTY])
        # call_tool reads the id via load_session_id() into a LOCAL, so the
        # module attribute is not the seam — patch the function.
        monkeypatch.setattr(mcp, "load_session_id", lambda: sid)
        out = await mcp.call_tool("check_git_changes", {})

    assert "No uncommitted changes" in out[0].text


# ── the contract between the two halves ─────────────────────────────────────

def test_the_tool_reads_a_key_the_response_model_actually_declares():
    """Structural pin on the mismatch itself. Both defects were invisible for
    as long as they were: nothing tied the reader's key to the writer's model."""
    from ai_team_sync.routers.git_status import SessionChangesResponse

    declared = set(SessionChangesResponse.model_fields)
    # Read the module file, not inspect.getsource(call_tool): call_tool is a
    # decorated wrapper and its source does not include the dispatch body.
    src = Path(mcp.__file__).read_text()
    handler = src[src.index('elif name == "check_git_changes"'):]
    handler = handler[:handler.index("elif name ==", 10)]

    read_keys = {
        line.split('data.get("')[1].split('"')[0]
        for line in handler.splitlines() if 'data.get("' in line
    }
    assert read_keys, "no data.get() found — the handler was restructured"
    assert read_keys <= declared, (
        f"handler reads {sorted(read_keys - declared)}, which "
        f"SessionChangesResponse does not declare {sorted(declared)}")
