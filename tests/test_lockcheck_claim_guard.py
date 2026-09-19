"""Claim guard (2026-08-17): the lockcheck's missing half.

find_conflicts asks "is someone ELSE on this file?" — these pin the new
question, "is MY session alive and does it claim the file?", which is what a
mid-turn reap silently broke (session auto-completed during a 25-minute
render turn, locks released, edits continued unguarded all day).
"""
import io
import json
import sys

import pytest

from ai_team_sync.hooks import pre_tool_use_lockcheck as guard
from ai_team_sync.hooks.pre_tool_use_lockcheck import claim_check, find_conflicts

REPO = "/opt/anime-studio"
REL = "packages/scene_generation/shot_composer_c2.py"

MY_SID = "d1948be4-3d13-42cf-be35-2b02a2718470"
MY_CID8 = "d8710a6c"


def _sess(sid=MY_SID, status="active", agent=f"claude-code:{MY_CID8}",
          scope=(), repo_root=REPO):
    return {"id": sid, "status": status, "agent": agent,
            "scope": list(scope), "repo_root": repo_root}


def _lock(sid=MY_SID, pattern=REL, repo_root=REPO):
    return {"session_id": sid, "pattern": pattern, "repo_root": repo_root}


def test_no_session_at_all_blocks_with_register_message():
    ok, reason = claim_check(REL, REPO, None, MY_CID8, [], [])
    assert not ok and "session start" in reason


def test_reaped_session_blocks_with_reregister_message():
    ok, reason = claim_check(REL, REPO, MY_SID, MY_CID8,
                             [_sess(status="completed")], [])
    assert not ok and "reaped" in reason and "locks are" in reason


def test_lock_covering_file_passes():
    ok, _ = claim_check(REL, REPO, MY_SID, MY_CID8, [_sess()], [_lock()])
    assert ok


def test_declared_scope_without_a_live_lock_does_not_grant_an_edit():
    ok, reason = claim_check(REL, REPO, MY_SID, MY_CID8,
                             [_sess(scope=["packages/scene_generation/**"])], [])
    assert not ok and "live lock" in reason


def test_own_live_lock_grants_even_when_scope_does_not_name_the_file():
    ok, reason = claim_check(REL, REPO, MY_SID, MY_CID8,
                             [_sess(scope=["docs/**"])], [_lock()])
    assert ok, reason


def test_active_but_unclaimed_blocks_with_take_a_lock():
    ok, reason = claim_check(REL, REPO, MY_SID, MY_CID8, [_sess()], [])
    assert not ok and "Take a lock" in reason


def test_lock_anchored_to_other_repo_does_not_cover():
    ok, _ = claim_check(REL, REPO, MY_SID, MY_CID8, [_sess()],
                        [_lock(repo_root="/opt/tower-echo-brain")])
    assert not ok


def test_another_sessions_lock_never_satisfies_my_claim():
    ok, _ = claim_check(REL, REPO, MY_SID, MY_CID8,
                        [_sess(), _sess(sid="other", agent="claude-code:beefcafe")],
                        [_lock(sid="other")])
    assert not ok


def test_agent_match_fallback_when_pointer_missing():
    """Pointer file absent (my_sid=None): the payload's Claude session id
    prefix still identifies my session by agent string."""
    ok, _ = claim_check(REL, REPO, None, MY_CID8, [_sess()], [_lock()])
    assert ok


def test_unanchored_lock_covers_legacy_rows():
    ok, _ = claim_check(REL, REPO, MY_SID, MY_CID8, [_sess()],
                        [_lock(repo_root="")])
    assert ok


def test_foreign_scope_without_lock_neither_blocks_nor_warns():
    other = _sess(sid="other", agent="codex:bbbbbbbb", scope=["packages/**"])
    assert find_conflicts(REL, [other], MY_CID8, REPO, locks=[]) == []


def test_foreign_live_locks_report_mode_even_outside_declared_scope():
    other = _sess(sid="other", agent="codex:bbbbbbbb", scope=["docs/**"])
    for mode in ("exclusive", "advisory"):
        hits = find_conflicts(REL, [other], MY_CID8, REPO,
                              locks=[dict(_lock(sid="other"), mode=mode)])
        assert len(hits) == 1
        assert hits[0][2:] == (REL, mode)


def test_lock_from_new_owner_is_not_lost_between_separate_api_reads():
    hits = find_conflicts(REL, [], MY_CID8, REPO,
                          locks=[dict(_lock(sid="new-session"), mode="exclusive",
                                      developer="new-owner")])
    assert hits == [("new-owner", "", REL, "exclusive")]


@pytest.mark.parametrize("other_mode,own_lock,coordinated,exit_code,message", [
    ("exclusive", False, False, 2, "LIVE EXCLUSIVE"),
    ("advisory", True, True, 0, "LIVE ADVISORY"),
    (None, False, True, 2, "no live lock"),
    (None, False, False, 0, ""),
    ("advisory", False, True, 2, "no live lock"),
])
def test_edit_hook_uses_live_locks_for_both_sides(
        monkeypatch, capsys, other_mode, own_lock, coordinated, exit_code, message):
    """The actual hook reads both endpoints; scope-only rows cannot decide."""
    import httpx
    from ai_team_sync import session_pointer

    sessions = [
        _sess(scope=["packages/**"]),
        _sess(sid="other", agent="codex:bbbbbbbb", scope=["docs/**"]),
    ]
    locks = []
    if own_lock:
        locks.append(dict(_lock(), mode="advisory"))
    if other_mode:
        locks.append(dict(_lock(sid="other"), mode=other_mode))

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
    monkeypatch.setattr(guard, "_roots", lambda _path: (REPO, REPO))
    monkeypatch.setattr(guard, "_coordinated_roots",
                        lambda: [REPO] if coordinated else [])
    monkeypatch.setattr(session_pointer, "resolve_pointer", lambda: MY_SID)
    monkeypatch.delenv("ATS_LOCKCHECK_BLOCK", raising=False)
    monkeypatch.delenv("ATS_CLAIMCHECK", raising=False)
    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps({
        "tool_name": "Edit", "session_id": MY_CID8,
        "tool_input": {"file_path": f"{REPO}/{REL}"},
    })))

    with pytest.raises(SystemExit) as stopped:
        guard.main()
    assert stopped.value.code == exit_code
    assert message in capsys.readouterr().err


def _invoke_edit_hook(monkeypatch, capsys, *, sessions, locks, sid=MY_SID,
                      cid=MY_CID8, coordinated=True, block=None):
    """Exercise main() with the two ATS read responses and Claude hook input."""
    import httpx
    from ai_team_sync import session_pointer

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
    monkeypatch.setattr(guard, "_roots", lambda _path: (REPO, REPO))
    monkeypatch.setattr(guard, "_coordinated_roots",
                        lambda: [REPO] if coordinated else [])
    monkeypatch.setattr(session_pointer, "resolve_pointer", lambda: sid)
    monkeypatch.delenv("ATS_CLAIMCHECK", raising=False)
    if block is None:
        monkeypatch.delenv("ATS_LOCKCHECK_BLOCK", raising=False)
    else:
        monkeypatch.setenv("ATS_LOCKCHECK_BLOCK", block)
    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps({
        "tool_name": "Edit", "session_id": cid,
        "tool_input": {"file_path": f"{REPO}/{REL}"},
    })))

    with pytest.raises(SystemExit) as stopped:
        guard.main()
    captured = capsys.readouterr()
    return stopped.value.code, captured.out, captured.err


def _agent_warning(stdout):
    """One JSON object, with the supported Claude Code PreToolUse context field."""
    parsed, end = json.JSONDecoder().raw_decode(stdout.lstrip())
    assert stdout.lstrip()[end:].strip() == ""
    assert set(parsed) == {"hookSpecificOutput"}
    specific = parsed["hookSpecificOutput"]
    assert specific["hookEventName"] == "PreToolUse"
    assert "permissionDecision" not in specific
    return specific["additionalContext"]


def test_main_advisory_warning_reaches_agent_and_lists_all_holders(monkeypatch, capsys):
    sessions = [_sess(),
                _sess(sid="other-1", agent="claude-code:other111"),
                _sess(sid="other-2", agent="claude-code:other222")]
    locks = [dict(_lock(), mode="advisory"),
             dict(_lock(sid="other-1"), mode="advisory"),
             dict(_lock(sid="other-2"), mode="advisory")]
    code, stdout, _stderr = _invoke_edit_hook(
        monkeypatch, capsys, sessions=sessions, locks=locks)
    assert code == 0
    warning = _agent_warning(stdout)
    assert "LIVE ADVISORY" in warning
    assert "other111" in warning and "other222" in warning


def test_main_warn_only_exclusive_reaches_agent_without_blocking(monkeypatch, capsys):
    sessions = [_sess(), _sess(sid="other", agent="claude-code:other111"),
                _sess(sid="adviser", agent="claude-code:other222")]
    locks = [dict(_lock(), mode="advisory"),
             dict(_lock(sid="other"), mode="exclusive"),
             dict(_lock(sid="adviser"), mode="advisory")]
    code, stdout, _stderr = _invoke_edit_hook(
        monkeypatch, capsys, sessions=sessions, locks=locks, block="0")
    assert code == 0
    warning = _agent_warning(stdout)
    assert "LIVE EXCLUSIVE" in warning and "other111" in warning
    assert "LIVE ADVISORY" in warning and "other222" in warning


def test_main_default_exclusive_still_blocks_a_lock_holder(monkeypatch, capsys):
    sessions = [_sess(), _sess(sid="other", agent="claude-code:other111")]
    locks = [dict(_lock(), mode="advisory"),
             dict(_lock(sid="other"), mode="exclusive")]
    code, stdout, stderr = _invoke_edit_hook(
        monkeypatch, capsys, sessions=sessions, locks=locks)
    assert code == 2
    assert "LIVE EXCLUSIVE" in stderr
    assert stdout == ""


def test_main_lost_lane_precedes_foreign_exclusive_inside_coordinated_repo(
        monkeypatch, capsys):
    loser = dict(_sess(scope=["packages/**"]), locks_not_restored=[REL])
    holder = _sess(sid="other", agent="claude-code:other111", scope=["docs/**"])
    locks = [dict(_lock(sid="other"), mode="exclusive")]

    code, _stdout, stderr = _invoke_edit_hook(
        monkeypatch, capsys, sessions=[loser, holder], locks=locks)
    assert code == 2
    assert "NOT restored" in stderr and "ATS CLAIM GUARD" in stderr

    code, _stdout, stderr = _invoke_edit_hook(
        monkeypatch, capsys, sessions=[loser, holder], locks=locks,
        sid="other", cid="other111")
    assert code == 0 and stderr == ""

    ordinary = _sess(scope=["packages/**"])
    code, _stdout, stderr = _invoke_edit_hook(
        monkeypatch, capsys, sessions=[ordinary, holder], locks=locks)
    assert code == 2
    assert "LIVE EXCLUSIVE" in stderr or "no live lock" in stderr
    assert "NOT restored" not in stderr


def test_main_noncoordinated_repo_does_not_run_lost_lane_claim_guard(
        monkeypatch, capsys):
    loser = dict(_sess(scope=["packages/**"]), locks_not_restored=[REL])
    holder = _sess(sid="other", agent="claude-code:other111")
    code, _stdout, stderr = _invoke_edit_hook(
        monkeypatch, capsys, sessions=[loser, holder],
        locks=[dict(_lock(sid="other"), mode="exclusive")], coordinated=False)
    assert code == 2
    assert "LIVE EXCLUSIVE" in stderr
    assert "NOT restored" not in stderr


async def test_ordinary_expiry_removes_both_claim_and_conflict(client, db_session):
    """A normal TTL expiry needs no resurrection journal to remove authority."""
    from datetime import datetime, timedelta, timezone
    from sqlalchemy import select
    from ai_team_sync.models import ScopeLock

    made = await client.post("/api/sessions", json={
        "agent": "default", "developer": "expiry-owner", "scope": ["src/**"],
        "auto_lock": True, "repo_root": REPO,
    })
    assert made.status_code == 201, made.text
    sid = made.json()["id"]
    row = (await db_session.execute(
        select(ScopeLock).where(ScopeLock.session_id == sid))).scalar_one()
    row.expires_at = datetime.now(timezone.utc) - timedelta(minutes=1)
    await db_session.commit()

    sessions = (await client.get("/api/sessions")).json()
    locks = (await client.get("/api/locks")).json()
    assert locks == []
    ok, reason = claim_check("src/a.py", REPO, sid, "", sessions, locks)
    assert not ok and "no live lock" in reason
    assert find_conflicts("src/a.py", sessions, "someone-else", REPO,
                          locks=locks) == []


async def test_scope_only_session_cannot_block_the_exclusive_holder(client):
    scope_only = await client.post("/api/sessions", json={
        "agent": "claude-code:aaaaaaaa", "developer": "intent-owner", "scope": ["src/**"],
        "auto_lock": False, "repo_root": REPO,
    })
    assert scope_only.status_code == 201, scope_only.text
    holder = await client.post("/api/sessions", json={
        "agent": "claude-code:bbbbbbbb", "developer": "lock-owner", "scope": ["docs/**"],
        "auto_lock": False, "repo_root": REPO,
    })
    assert holder.status_code == 201, holder.text
    acquired = await client.post("/api/locks", json={
        "session_id": holder.json()["id"], "pattern": "src/a.py",
        "mode": "exclusive", "reason": "the actual edit lane",
    })
    assert acquired.status_code == 201, acquired.text

    sessions = (await client.get("/api/sessions")).json()
    locks = (await client.get("/api/locks")).json()
    sid = scope_only.json()["id"]
    holder_sid = holder.json()["id"]
    assert claim_check("src/a.py", REPO, sid, "", sessions, locks)[0] is False
    assert claim_check("src/a.py", REPO, holder_sid, "", sessions, locks)[0] is True
    assert find_conflicts("src/a.py", sessions, "aaaaaaaa", REPO,
                          locks=locks) == [
        ("claude-code:bbbbbbbb", "", "src/a.py", "exclusive")]
    assert find_conflicts("src/a.py", sessions, "bbbbbbbb", REPO,
                          locks=locks) == []
