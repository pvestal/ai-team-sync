"""Reaping a session must NAME the work it strands, and a session must be able
to anchor itself after auto-registration (#2554).

THE INCIDENT. 2026-08-23 on /opt/anime-studio: four sessions were auto-completed
and their locks released, leaving 10 uncommitted files in the shared tree — four
real fixes plus their passing tests. Nothing surfaced them. The next session's
`git add -A` would have swept that work into an unrelated commit; a checkout
would have destroyed it. It was found by hand, by someone reading `git status`
and wondering whose it was.

Three defects formed one chain:
  1. SessionUpdate had no repo_root, so PATCH silently dropped it. Every session
     begins auto-registered with repo_root='' (Gap 0), so there was NO API path
     from the default state to an anchored session.
  2. uncommitted_in_scope returns [] for an unanchored session, so (1) made the
     field dead in practice. It is also computed only for status=='active', so
     the signal becomes unreachable the instant the reaper flips status.
  3. auto_complete_stale_sessions never consulted it before completing.

These tests pin the chain closed at each link.
"""
from __future__ import annotations

import subprocess
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import select

from ai_team_sync.background_tasks import auto_complete_stale_sessions
from ai_team_sync.config import settings
from ai_team_sync.models import Session


def _utcnow():
    return datetime.now(timezone.utc)


def _repo_with_dirty(tmp_path, files):
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


# ── Link 1: a session can anchor itself after the fact ──────────────────────

@pytest.mark.asyncio
async def test_patch_can_anchor_an_auto_registered_session(client, tmp_path):
    """The defect verbatim: PATCH returned 200 and repo_root stayed ''."""
    repo = _repo_with_dirty(tmp_path, ["src/a.py"])

    created = await client.post("/api/sessions", json={
        "developer": "anchor-tester", "agent": "claude",
        "scope": [], "description": "auto-registered, unanchored",
    })
    assert created.status_code in (200, 201), created.text
    sid = created.json()["id"]

    patched = await client.patch(f"/api/sessions/{sid}", json={
        "scope": ["src/**"], "repo_root": str(repo),
    })
    assert patched.status_code == 200, patched.text

    listed = await client.get("/api/sessions", params={"status": "active"})
    (s,) = [x for x in listed.json() if x["developer"] == "anchor-tester"]
    # Anchoring is only worth anything if it makes the dirty file visible.
    assert s["uncommitted_in_scope"] == ["src/a.py"]


@pytest.mark.asyncio
async def test_anchor_is_stored_without_a_trailing_slash(client, tmp_path):
    """repo_root is compared by string equality in the repo-anchoring paths, so
    '/repo/' and '/repo' must not read as two different repos."""
    repo = _repo_with_dirty(tmp_path, ["src/a.py"])

    created = await client.post("/api/sessions", json={
        "developer": "slash-tester", "agent": "claude", "scope": ["src/**"]})
    sid = created.json()["id"]
    await client.patch(f"/api/sessions/{sid}", json={"repo_root": str(repo) + "/"})

    listed = await client.get("/api/sessions", params={"status": "active"})
    (s,) = [x for x in listed.json() if x["developer"] == "slash-tester"]
    assert s["uncommitted_in_scope"] == ["src/a.py"]


# ── Link 3: the reaper looks before it completes ────────────────────────────

@pytest.mark.asyncio
async def test_reaper_names_the_files_it_strands(db_session, tmp_path):
    repo = _repo_with_dirty(tmp_path, ["src/keep.py", "src/also.py", "docs/out.md"])
    stale = _utcnow() - timedelta(hours=settings.session_inactivity_hours + 1)

    sess = Session(developer="patrick", agent="claude-code", status="active",
                   started_at=stale, repo_root=str(repo), scope='["src/**"]')
    db_session.add(sess)
    await db_session.commit()

    assert await auto_complete_stale_sessions(db_session) == 1

    row = (await db_session.execute(
        select(Session).where(Session.id == sess.id))).scalar_one()
    assert row.status == "completed"
    # Reap anyway — holding a dead session's locks blocks the lane for everyone.
    # What must change is that the stranded work is NAMED.
    assert "STRANDED 2 uncommitted file(s)" in (row.summary or "")
    assert "src/keep.py" in row.summary and "src/also.py" in row.summary
    # Out of scope is not this session's to claim.
    assert "docs/out.md" not in row.summary


@pytest.mark.asyncio
async def test_reaper_stays_quiet_when_nothing_was_stranded(db_session, tmp_path):
    """A clean session must not gain a scary STRANDED note — the signal is only
    worth anything if its absence means something."""
    repo = _repo_with_dirty(tmp_path, [])
    stale = _utcnow() - timedelta(hours=settings.session_inactivity_hours + 1)

    sess = Session(developer="patrick", agent="claude-code", status="active",
                   started_at=stale, repo_root=str(repo), scope='["src/**"]')
    db_session.add(sess)
    await db_session.commit()

    assert await auto_complete_stale_sessions(db_session) == 1
    row = (await db_session.execute(
        select(Session).where(Session.id == sess.id))).scalar_one()
    assert "auto-completed" in (row.summary or "")
    assert "STRANDED" not in (row.summary or "")


@pytest.mark.asyncio
async def test_a_dead_git_root_never_wedges_the_sweep(db_session, tmp_path):
    """The reaper's job is releasing locks. A git call that explodes on one
    session must not stop the others being reaped."""
    stale = _utcnow() - timedelta(hours=settings.session_inactivity_hours + 1)
    sess = Session(developer="patrick", agent="claude-code", status="active",
                   started_at=stale, repo_root=str(tmp_path / "does-not-exist"),
                   scope='["src/**"]')
    db_session.add(sess)
    await db_session.commit()

    assert await auto_complete_stale_sessions(db_session) == 1
    row = (await db_session.execute(
        select(Session).where(Session.id == sess.id))).scalar_one()
    assert row.status == "completed"
