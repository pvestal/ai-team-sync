"""Claim guard (2026-08-17): the lockcheck's missing half.

find_conflicts asks "is someone ELSE on this file?" — these pin the new
question, "is MY session alive and does it claim the file?", which is what a
mid-turn reap silently broke (session auto-completed during a 25-minute
render turn, locks released, edits continued unguarded all day).
"""
from ai_team_sync.hooks.pre_tool_use_lockcheck import claim_check

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


def test_scope_covering_file_passes():
    ok, _ = claim_check(REL, REPO, MY_SID, MY_CID8,
                        [_sess(scope=["packages/scene_generation/**"])], [])
    assert ok


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
