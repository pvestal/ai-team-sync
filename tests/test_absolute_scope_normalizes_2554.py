"""An ABSOLUTE scope pattern must claim the file it obviously names (#2554).

ATS scope is repo-RELATIVE, but nothing said so at the point of writing it and
nothing caught it afterwards. The guard compares against a repo-relative `rel`,
so a scope declared as '/opt/anime-studio/packages/x.py' matched NOTHING:
fnmatch('packages/x.py', '/opt/anime-studio/packages/x.py') is False.

Observed 2026-08-23: a session set scope to absolute paths, got a 200, was then
blocked from editing those exact files, and read "your active ATS session holds
no lock or scope covering X" — a message that names neither the path form nor
the fix. So it took locks instead of correcting the scope, and the broken scope
stayed broken. That is a coordination failure dressed as a permissions one.

Two halves pinned here: absolute patterns now RESOLVE, and when a block does
happen with an absolute pattern present, the reason SAYS SO and gives the exact
replacement.
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

def test_absolute_scope_now_claims_the_file():
    ok, reason = claim_check(REL, REPO, MY_SID, MY_CID8,
                             [_sess(scope=[ABS])], [])
    assert ok, reason


def test_absolute_scope_still_conflicts_for_other_sessions():
    """The other half of coordination: if an absolute pattern claims a file for
    ME, it must also warn me off someone ELSE's file. A one-sided fix would let
    two sessions both believe they held it."""
    other = _sess(sid="other-sid", agent="codex:aaaaaaaa", scope=[ABS])
    hits = find_conflicts(REL, [other], MY_SID, file_repo_root=REPO)
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
