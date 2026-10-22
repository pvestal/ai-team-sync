"""Parallel subagents of ONE Claude session, and locks anchored to a worktree.

Observed 2026-10-22 (Claude session 7951905d): the lead and four background
subagents share one CLAUDE_CODE_SESSION_ID, so they share ONE per-session
pointer (~/.ats_session_<cid8>). Each subagent's start_session overwrote it, and
the edit guard -- which resolves identity pointer-first -- judged subagent A's
Write as subagent B's session: "your active ATS session holds no live lock
covering ...". A's session could then only be released by the reaper.

The rule pinned here: when the pointer session holds no covering lock, a live
lock held by another ACTIVE session whose agent label is EXACTLY this Claude
session's canonical label (session_pointer.agent_label -> 'claude-code:<cid8>')
grants the edit. Exact equality only -- substring matching is the open
decision on #2893 and is NOT widened here. Scope never grants (#2813).

Second defect: a lock created with repo_root = a LINKED WORKTREE could never
satisfy the guard, because the guard resolves a file in that worktree to the
main checkout's root and compared roots by string equality, while the server
treats a worktree as its main checkout.
"""
from __future__ import annotations

import io
import json
import os
import subprocess
import sys

import pytest

from ai_team_sync.hooks import pre_tool_use_lockcheck as guard
from ai_team_sync.hooks.pre_tool_use_lockcheck import claim_check, find_conflicts

REPO = "/opt/anime-studio"
REL = "packages/scene_generation/shot_composer_c2.py"

CID = "7951905d-99c1-46d9-9f61-528b785ae279"
CID8 = CID[:8]
LABEL = f"claude-code:{CID8}"

SUB_A = "aaaaaaaa-0000-4000-8000-00000000000a"   # subagent A: holds the lock
SUB_B = "bbbbbbbb-0000-4000-8000-00000000000b"   # subagent B: owns the pointer


def _sess(sid, agent=LABEL, status="active", scope=(), repo_root=REPO):
    return {"id": sid, "status": status, "agent": agent, "scope": list(scope),
            "repo_root": repo_root, "description": f"session {sid[:8]}"}


def _lock(sid, pattern=REL, mode="advisory", repo_root=REPO, lock_id=None):
    return {"id": lock_id or f"lock-{sid[:8]}", "session_id": sid, "pattern": pattern,
            "mode": mode, "repo_root": repo_root}


@pytest.fixture(autouse=True)
def _claude_identity(monkeypatch):
    """This hook runs as a Claude Code session; declared here, never inherited."""
    monkeypatch.setenv("ATS_AGENT", "claude-code")
    for var in ("ATS_DELEGATION", "ATS_SESSION_ID", "ATS_LOCKCHECK_BLOCK",
                "ATS_CLAIMCHECK", "CLAUDE_CODE_SESSION_ID", "ATS_SESSION"):
        monkeypatch.delenv(var, raising=False)


def _pointer_identity(monkeypatch, sid):
    from ai_team_sync.hooks import commit_guard

    ident = (commit_guard.Identity(sid, "per_session") if sid
             else commit_guard.Identity(None, "none"))
    monkeypatch.setattr(commit_guard, "resolve_identity", lambda *_a, **_kw: ident)


def _run_hook(monkeypatch, capsys, *, sessions, locks, file_path, pointer=SUB_B,
              cid=CID, coordinated=(REPO,), roots=None):
    import httpx

    class Response:
        def __init__(self, body):
            self.body = body

        def raise_for_status(self):
            pass

        def json(self):
            return self.body

    class Client:
        def __init__(self, **_kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def get(self, url):
            return Response(locks if url.endswith("/api/locks") else sessions)

    monkeypatch.setattr(httpx, "Client", Client)
    if roots is not None:
        monkeypatch.setattr(guard, "_roots", lambda _path: roots)
    else:
        # Real repositories under pytest's tmp_path live in /tmp, which the
        # hook skips as noise; the noise filter is not what these tests probe.
        monkeypatch.setattr(guard, "_is_noise", lambda _path: False)
    monkeypatch.setattr(guard, "_coordinated_roots", lambda: list(coordinated))
    _pointer_identity(monkeypatch, pointer)
    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps({
        "tool_name": "Write", "session_id": cid,
        "tool_input": {"file_path": file_path}})))
    with pytest.raises(SystemExit) as stopped:
        guard.main()
    captured = capsys.readouterr()
    return stopped.value.code, captured.out, captured.err


# ── rule 2: an exact-label sibling's live lock grants ─────────────────────────

@pytest.mark.parametrize("mode", ["advisory", "exclusive"])
def test_subagent_a_edits_under_its_own_lock_while_the_pointer_names_subagent_b(
        monkeypatch, capsys, mode):
    """The 2026-10-22 refusal, reproduced: B's start_session took the shared
    pointer, A's Write is judged as B's. A's lock is a lock of this same Claude
    session, so the edit is granted, and A's own lock is not reported back to
    it as a foreign conflict."""
    sessions = [_sess(SUB_A), _sess(SUB_B)]
    locks = [_lock(SUB_A, mode=mode)]
    code, _out, err = _run_hook(monkeypatch, capsys, sessions=sessions, locks=locks,
                                file_path=f"{REPO}/{REL}", roots=(REPO, REPO))
    assert code == 0, err
    assert "LIVE EXCLUSIVE" not in err and "no live lock" not in err


@pytest.mark.parametrize("label", [
    f"claude-code:{CID8}x",          # suffix
    f"xclaude-code:{CID8}",          # prefix
    f"codex:{CID8}",                 # same cid8, different base
    CID8,                            # bare cid8
    "claude-code",                   # bare base
    f"claude-code:{CID8}:delegate",  # delegated child of this session
])
def test_a_label_that_only_contains_this_sessions_cid_never_grants(
        monkeypatch, capsys, label):
    """Exact equality only. #2893 is the open decision about substring
    matching; this rule must not decide it by the back door."""
    sessions = [_sess(SUB_A, agent=label), _sess(SUB_B)]
    locks = [_lock(SUB_A)]
    code, _out, err = _run_hook(monkeypatch, capsys, sessions=sessions, locks=locks,
                                file_path=f"{REPO}/{REL}", roots=(REPO, REPO))
    assert code == 2
    assert "holds no live lock" in err


def test_another_claude_sessions_lock_still_refuses_and_still_blocks(monkeypatch, capsys):
    other = "cccccccc-0000-4000-8000-00000000000c"
    sessions = [_sess(other, agent="claude-code:0badf00d"), _sess(SUB_B)]
    locks = [_lock(other, mode="exclusive")]
    code, _out, err = _run_hook(monkeypatch, capsys, sessions=sessions, locks=locks,
                                file_path=f"{REPO}/{REL}", roots=(REPO, REPO))
    assert code == 2
    assert "holds no live lock" in err


def test_another_claude_sessions_exclusive_lock_blocks_outside_governed_repos(
        monkeypatch, capsys):
    other = "cccccccc-0000-4000-8000-00000000000c"
    sessions = [_sess(other, agent="claude-code:0badf00d"), _sess(SUB_B)]
    locks = [_lock(other, mode="exclusive")]
    code, _out, err = _run_hook(monkeypatch, capsys, sessions=sessions, locks=locks,
                                file_path=f"{REPO}/{REL}", roots=(REPO, REPO),
                                coordinated=())
    assert code == 2 and "LIVE EXCLUSIVE" in err and "0badf00d" in err


@pytest.mark.parametrize("status", ["completed", "paused", "cancelled"])
def test_a_sibling_that_is_not_active_never_grants(monkeypatch, capsys, status):
    sessions = [_sess(SUB_A, status=status), _sess(SUB_B)]
    locks = [_lock(SUB_A)]
    code, _out, err = _run_hook(monkeypatch, capsys, sessions=sessions, locks=locks,
                                file_path=f"{REPO}/{REL}", roots=(REPO, REPO))
    assert code == 2 and "holds no live lock" in err


def test_a_siblings_scope_never_grants(monkeypatch, capsys):
    """#2813: scope is intent. A sibling that only DECLARED the file grants nothing."""
    sessions = [_sess(SUB_A, scope=["packages/**"]), _sess(SUB_B)]
    code, _out, err = _run_hook(monkeypatch, capsys, sessions=sessions, locks=[],
                                file_path=f"{REPO}/{REL}", roots=(REPO, REPO))
    assert code == 2 and "holds no live lock" in err


def test_a_siblings_lock_on_another_file_never_grants(monkeypatch, capsys):
    sessions = [_sess(SUB_A), _sess(SUB_B)]
    locks = [_lock(SUB_A, pattern="docs/**")]
    code, _out, err = _run_hook(monkeypatch, capsys, sessions=sessions, locks=locks,
                                file_path=f"{REPO}/{REL}", roots=(REPO, REPO))
    assert code == 2 and "holds no live lock" in err


def test_a_siblings_lock_in_another_repository_never_grants(monkeypatch, capsys):
    sessions = [_sess(SUB_A, repo_root="/opt/tower-echo-brain"), _sess(SUB_B)]
    locks = [_lock(SUB_A, repo_root="/opt/tower-echo-brain")]
    code, _out, err = _run_hook(monkeypatch, capsys, sessions=sessions, locks=locks,
                                file_path=f"{REPO}/{REL}", roots=(REPO, REPO))
    assert code == 2 and "holds no live lock" in err


def test_a_delegated_child_gets_no_sibling_grant(monkeypatch, capsys):
    """A delegated child's label is the bare base (session_pointer.agent_label),
    which carries no Claude session id; the sibling rule needs one."""
    monkeypatch.setenv("ATS_DELEGATION", "dddd0000-0000-0000-0000-000000000000")
    sessions = [_sess(SUB_A), _sess(SUB_B)]
    locks = [_lock(SUB_A)]
    code, _out, err = _run_hook(monkeypatch, capsys, sessions=sessions, locks=locks,
                                file_path=f"{REPO}/{REL}", roots=(REPO, REPO))
    assert code == 2


def test_the_reaped_pointer_message_is_kept_when_no_sibling_lock_covers(monkeypatch, capsys):
    sessions = [_sess(SUB_A), _sess(SUB_B, status="completed")]
    code, _out, err = _run_hook(monkeypatch, capsys, sessions=sessions, locks=[],
                                file_path=f"{REPO}/{REL}", roots=(REPO, REPO))
    assert code == 2 and "completed/reaped" in err


def test_the_pure_functions_keep_exact_identity_without_a_sibling_label():
    """The #3581/ef62ab1 contract for callers that do not opt in: a same-label
    foreign lock stays a conflict and grants nothing."""
    sessions = [_sess(SUB_B), _sess(SUB_A)]
    locks = [_lock(SUB_A, mode="exclusive")]
    assert find_conflicts(REL, sessions, CID8, REPO, locks=locks, my_ats_session_id=SUB_B)
    assert not claim_check(REL, REPO, SUB_B, CID8, sessions, locks)[0]
    ok, reason = claim_check(REL, REPO, SUB_B, CID8, sessions, locks, sibling_label=LABEL)
    assert ok, reason
    assert find_conflicts(REL, sessions, CID8, REPO, locks=locks, my_ats_session_id=SUB_B,
                          sibling_session_ids={SUB_A}) == []


# ── worktree anchoring ────────────────────────────────────────────────────────

def _git(cwd, *args):
    subprocess.run(["git", "-C", str(cwd), "-c", "user.email=t@t", "-c", "user.name=t",
                    *args], check=True, capture_output=True, text=True)


@pytest.fixture
def worktree(tmp_path):
    """A real main checkout and a real `git worktree add` of it."""
    main = tmp_path / "main"
    main.mkdir()
    _git(main, "init", "-q", "-b", "main")
    (main / "packages").mkdir()
    (main / "packages" / "x.py").write_text("x = 1\n")
    _git(main, "add", "-A")
    _git(main, "commit", "-q", "-m", "init")
    linked = tmp_path / "wt" / "line-delivery-mode"
    _git(main, "worktree", "add", "-q", "-b", "line-delivery-mode", str(linked))
    return str(main.resolve()), str(linked.resolve())


def test_resolve_repo_roots_names_the_main_checkout_for_a_worktree_file(worktree):
    from ai_team_sync.git_utils import resolve_repo_roots, shared_repo_root

    main, linked = worktree
    assert resolve_repo_roots(os.path.join(linked, "packages", "x.py")) == (linked, main)
    assert shared_repo_root(linked) == main
    assert shared_repo_root(linked + "/") == main
    assert shared_repo_root(main) == main
    assert shared_repo_root("") == ""
    assert shared_repo_root("/no/such/dir") == "/no/such/dir"


def test_a_lock_anchored_to_the_worktree_covers_its_own_files(worktree, monkeypatch, capsys):
    """The 2026-10-22 dead end: start_session(repo_root=<linked worktree>) took
    a lock that could never satisfy the guard, because the file resolved to
    the main checkout's root and roots were compared as strings."""
    main, linked = worktree
    sid = SUB_B
    sessions = [_sess(sid, repo_root=linked)]
    locks = [_lock(sid, pattern="packages/x.py", repo_root=linked)]
    code, _out, err = _run_hook(monkeypatch, capsys, sessions=sessions, locks=locks,
                                file_path=os.path.join(linked, "packages", "x.py"),
                                pointer=sid, coordinated=(main,))
    assert code == 0, err


def test_a_sibling_lock_anchored_to_the_worktree_grants_too(worktree, monkeypatch, capsys):
    main, linked = worktree
    sessions = [_sess(SUB_A, repo_root=linked), _sess(SUB_B, repo_root=main)]
    locks = [_lock(SUB_A, pattern="packages/x.py", repo_root=linked)]
    code, _out, err = _run_hook(monkeypatch, capsys, sessions=sessions, locks=locks,
                                file_path=os.path.join(linked, "packages", "x.py"),
                                coordinated=(main,))
    assert code == 0, err


def test_a_foreign_lock_anchored_to_the_worktree_conflicts_in_the_main_checkout(
        worktree, monkeypatch, capsys):
    """The other direction of the same comparison: a foreign EXCLUSIVE lock
    taken from the worktree was silently skipped as 'another repository' for
    the same file in the main checkout, while the server calls it a conflict."""
    main, linked = worktree
    other = "cccccccc-0000-4000-8000-00000000000c"
    sessions = [_sess(other, agent="codex:0badf00d", repo_root=linked),
                _sess(SUB_B, repo_root=main)]
    locks = [_lock(other, pattern="packages/x.py", mode="exclusive", repo_root=linked),
             _lock(SUB_B, pattern="packages/x.py", repo_root=main)]
    code, _out, err = _run_hook(monkeypatch, capsys, sessions=sessions, locks=locks,
                                file_path=os.path.join(main, "packages", "x.py"),
                                coordinated=(main,))
    assert code == 2 and "LIVE EXCLUSIVE" in err and "0badf00d" in err


def test_a_lock_in_an_unrelated_repository_still_does_not_cover(worktree, tmp_path,
                                                                monkeypatch, capsys):
    main, linked = worktree
    unrelated = tmp_path / "unrelated"
    unrelated.mkdir()
    _git(unrelated, "init", "-q", "-b", "main")
    sessions = [_sess(SUB_B, repo_root=str(unrelated))]
    locks = [_lock(SUB_B, pattern="packages/x.py", repo_root=str(unrelated))]
    code, _out, err = _run_hook(monkeypatch, capsys, sessions=sessions, locks=locks,
                                file_path=os.path.join(linked, "packages", "x.py"),
                                pointer=SUB_B, coordinated=(main,))
    assert code == 2 and "holds no live lock" in err
