"""#3591 (operator 2026-11-24): readers -- Echo preflight -- can take the
complete decision and Codex review history of one repository: linked
worktrees anywhere count, admission records can be left out, and the review
list pages to its end instead of stopping at a fixed window.

Additive query parameters only; existing parameters behave as before.
"""
from __future__ import annotations

import subprocess
from datetime import datetime, timedelta

import pytest

from ai_team_sync.git_utils import repository_of
from ai_team_sync.models import Decision, Delegation, Session
from ai_team_sync.routers import decisions as decisions_router
from ai_team_sync.routers import delegations as delegations_router


def _git(cwd, *args):
    subprocess.run(["git", "-C", str(cwd), "-c", "user.email=t@example.invalid",
                    "-c", "user.name=t", *args], check=True, capture_output=True)


@pytest.fixture
def repo(tmp_path):
    root = tmp_path / "repo"
    root.mkdir()
    _git(root, "init", "-q", "-b", "main")
    (root / "a.py").write_text("x = 1\n")
    _git(root, "add", "-A")
    _git(root, "commit", "-q", "-m", "init")
    wt = tmp_path / "elsewhere" / "wt"
    _git(root, "worktree", "add", "-q", "-b", "feat/x", str(wt))
    return str(root.resolve()), str(wt.resolve())


def test_repository_of_maps_a_worktree_anywhere_to_its_checkout(repo, tmp_path):
    root, wt = repo
    assert repository_of(wt) == root
    assert repository_of(root) == root
    assert repository_of(str(tmp_path / "gone" / "r" / ".worktrees" / "x")) == \
        str(tmp_path / "gone" / "r")
    assert repository_of("") == ""


def _session(sid, root, ts):
    return Session(id=sid, developer="d", agent="a", scope="[]", description="x",
                   status="active", branch="", started_at=ts, repo_root=root)


def _decision(did, sid, ts, kind="decision"):
    return Decision(id=did, session_id=sid, title=did, chosen="c", reasoning="r", files="[]",
                    created_at=ts, record_kind=kind)


@pytest.mark.asyncio
async def test_decisions_by_repository_include_worktrees_and_skip_admission(
        client, db_session, repo, tmp_path):
    root, wt = repo
    t0 = datetime(2026, 11, 1)
    other = str(tmp_path)
    db_session.add_all([_session("s-root", root, t0), _session("s-wt", wt, t0),
                        _session("s-other", other, t0)])
    db_session.add_all([_decision("d-root", "s-root", t0), _decision("d-wt", "s-wt", t0),
                        _decision("d-other", "s-other", t0),
                        _decision("d-adm", "s-root", t0, kind="work_admission")])
    await db_session.commit()
    rows = (await client.get("/api/decisions", params={
        "repository": root, "record_kind": "decision"})).json()
    assert sorted(r["title"] for r in rows) == ["d-root", "d-wt"]
    assert {r["record_kind"] for r in rows} == {"decision"}
    # Existing exact filter unchanged; record_kind visible on every row.
    exact = (await client.get("/api/decisions", params={"repo_root": root})).json()
    assert sorted(r["title"] for r in exact) == ["d-adm", "d-root"]
    assert {r["record_kind"] for r in exact} == {"decision", "work_admission"}


@pytest.mark.asyncio
async def test_repository_matches_past_the_first_scan_batch(client, db_session, repo,
                                                            tmp_path, monkeypatch):
    root, _wt = repo
    monkeypatch.setattr(decisions_router, "_SCAN_BATCH", 2)
    t0 = datetime(2026, 11, 1)
    db_session.add_all([_session("s-root", root, t0), _session("s-other", str(tmp_path), t0)])
    # Newest five belong to another repo; the match is in the third batch.
    db_session.add(_decision("d-root", "s-root", t0))
    for i in range(5):
        db_session.add(_decision(f"d-o{i}", "s-other", t0 + timedelta(hours=i + 1)))
    await db_session.commit()
    rows = (await client.get("/api/decisions", params={"repository": root})).json()
    assert [r["title"] for r in rows] == ["d-root"]


def _delegation(did, parent, repo_root, ts, verdict="BLOCK", mode="VERIFY"):
    return Delegation(
        id=did, parent_session_id=parent, parent_task="t", delegating_worker="c",
        delegated_worker="codex", mode=mode, repo_root=repo_root, scope="[]", objective="o",
        acceptance="", prohibitions="", lease_expires_at=ts, state="returned",
        result_summary="", evidence="", verdict="", effective_verdict=verdict, created_at=ts)


@pytest.mark.asyncio
async def test_review_history_pages_to_the_end_with_before(client, db_session, repo,
                                                           tmp_path, monkeypatch):
    root, wt = repo
    monkeypatch.setattr(delegations_router, "_SCAN_BATCH", 2)
    t0 = datetime(2026, 11, 1)
    db_session.add_all([_session("p-root", root, t0), _session("p-other", str(tmp_path), t0)])
    want = []
    for i in range(9):
        ts = t0 + timedelta(hours=i)
        if i % 3 == 0:
            db_session.add(_delegation(f"x{i}", "p-other", str(tmp_path), ts))   # other repo
        elif i == 4:
            db_session.add(_delegation(f"m{i}", "p-root", wt, ts, verdict="PASS"))  # not BLOCK
        elif i == 5:
            # reviewed in a worktree that has since been removed: parent says the repo
            db_session.add(_delegation(f"r{i}", "p-root", str(tmp_path / "removed-wt"), ts))
            want.append(f"r{i}")
        else:
            db_session.add(_delegation(f"r{i}", "p-root", wt, ts))
            want.append(f"r{i}")
    await db_session.commit()
    seen, before = [], None
    while True:
        params = {"mode": "VERIFY", "effective_verdict": "BLOCK", "repository": root,
                  "limit": 2, **({"before": before} if before else {})}
        page = (await client.get("/api/delegations", params=params)).json()
        seen += [d["id"] for d in page]
        if len(page) < 2:
            break
        before = page[-1]["created_at"]
    assert seen == list(reversed(want))


@pytest.mark.asyncio
async def test_delegations_without_new_parameters_are_unchanged(client, db_session, tmp_path):
    t0 = datetime(2026, 11, 1)
    db_session.add(_session("p", str(tmp_path), t0))
    for i in range(105):
        db_session.add(_delegation(f"d{i:03d}", "p", str(tmp_path), t0 + timedelta(minutes=i),
                                   mode="IMPLEMENT", verdict=""))
    await db_session.commit()
    rows = (await client.get("/api/delegations")).json()
    assert len(rows) == 100 and rows[0]["id"] == "d104"


@pytest.mark.asyncio
async def test_codex_53a2ceeb_rows_sharing_a_timestamp_across_pages_are_all_read(
        client, db_session, repo):
    """Five reviews with ONE created_at, pages of two: every row exactly once."""
    root, wt = repo
    ts = datetime(2026, 11, 1, 12, 0, 0)
    db_session.add(_session("p-root", root, ts))
    for i in range(5):
        db_session.add(_delegation(f"t{i}", "p-root", wt, ts))
    await db_session.commit()
    for repository in ("", root):   # plain list and repository-filtered list
        seen, cursor = [], {}
        while True:
            page = (await client.get("/api/delegations", params={
                "mode": "VERIFY", "limit": 2, **({"repository": repository} if repository else {}),
                **cursor})).json()
            seen += [d["id"] for d in page]
            if len(page) < 2:
                break
            cursor = {"before": page[-1]["created_at"], "before_id": page[-1]["id"]}
        assert seen == ["t4", "t3", "t2", "t1", "t0"], repository
