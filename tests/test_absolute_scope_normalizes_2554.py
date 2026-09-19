"""Absolute patterns under a repo normalize before matching (#2554).

#2813 makes scope intent only. The hook still normalizes an absolute lock
pattern under its repo and names an absolute scope pattern in a missing-lock
diagnostic. Correcting a declared scope alone does not authorize an edit.
"""
from ai_team_sync.hooks.pre_tool_use_lockcheck import (
    claim_check,
    find_conflicts,
    normalize_pattern,
    scope_matches,
)

REPO = "/opt/anime-studio"
REL = "packages/scene_generation/scene_review.py"
ABS = f"{REPO}/{REL}"

MY_SID = "d1948be4-3d13-42cf-be35-2b02a2718470"
MY_CID8 = "d8710a6c"


def _sess(sid=MY_SID, status="active", agent=f"claude-code:{MY_CID8}",
          scope=(), repo_root=REPO):
    return {"id": sid, "status": status, "agent": agent,
            "scope": list(scope), "repo_root": repo_root}


# ── normalize_pattern ───────────────────────────────────────────────────────

def test_absolute_under_this_repo_becomes_relative():
    assert normalize_pattern(ABS, REPO) == REL


def test_absolute_glob_under_this_repo_keeps_its_glob():
    assert normalize_pattern(f"{REPO}/packages/**", REPO) == "packages/**"


def test_relative_pattern_is_untouched():
    assert normalize_pattern(REL, REPO) == REL
    assert normalize_pattern("packages/**", REPO) == "packages/**"


def test_absolute_outside_this_repo_is_left_alone():
    """Re-rooting a foreign path would INVENT a claim — '/other/pkg/x.py' does
    not describe anything in this repo and must keep failing to match."""
    foreign = "/opt/tower-echo-brain/src/x.py"
    assert normalize_pattern(foreign, REPO) == foreign
    assert not scope_matches("src/x.py", foreign, REPO)


def test_unknown_repo_root_leaves_absolute_unresolved():
    """With no repo_root there is nothing to strip, and guessing would be worse
    than blocking — the caller can still take a lock."""
    assert normalize_pattern(ABS, "") == ABS
    assert not scope_matches(REL, ABS, "")


# ── the guard actually honours it ───────────────────────────────────────────

def test_absolute_scope_does_not_claim_the_file_without_a_lock():
    ok, reason = claim_check(REL, REPO, MY_SID, MY_CID8,
                             [_sess(scope=[ABS])], [])
    assert not ok and "live lock" in reason


def test_absolute_lock_claims_the_file():
    ok, reason = claim_check(REL, REPO, MY_SID, MY_CID8,
                             [_sess(scope=[])],
                             [{"session_id": MY_SID, "pattern": ABS}])
    assert ok, reason


def test_absolute_lock_still_conflicts_for_other_sessions():
    other = _sess(sid="other-sid", agent="codex:aaaaaaaa", scope=[ABS])
    hits = find_conflicts(REL, [other], MY_SID, file_repo_root=REPO,
                          locks=[{"session_id": "other-sid", "pattern": ABS,
                                  "mode": "exclusive"}])
    assert len(hits) == 1 and hits[0][2] == ABS


def test_block_reason_names_the_absolute_pattern_and_the_fix():
    unrelated_abs = f"{REPO}/packages/other/thing.py"
    ok, reason = claim_check(REL, REPO, MY_SID, MY_CID8,
                             [_sess(scope=[unrelated_abs])], [])
    assert not ok
    assert "ABSOLUTE" in reason
    assert unrelated_abs in reason                      # what is wrong
    assert "packages/other/thing.py" in reason          # what to write instead


def test_plain_miss_still_says_patterns_are_relative():
    ok, reason = claim_check(REL, REPO, MY_SID, MY_CID8,
                             [_sess(scope=["packages/other/**"])], [])
    assert not ok
    assert "repo-relative" in reason
