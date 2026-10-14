"""Cross-project authority is enforced where operations happen (ruling 2026-10-14).

The ATS-first prompt hook no longer refuses prompts that name another governed
project. What stops an anchored session from acting there is the operation
guards, run here as Claude Code and git run them: the real ATS app on loopback
(tests.test_hook_delivery_live.live_ats), real git repositories with the ATS git
hooks installed (tests.test_commit_guard_live.world), and each hook as a
subprocess.

Session X is anchored to the ungoverned repository (the shape of the 2026-10-14
incident: an ATS-repo session) and holds a lock there. It then tries every kind
of operation in the governed repository. Session Y is the structured switch: a
session anchored to the governed repository holding a lock on h.txt.

Tests named *_residual pin documented gaps (docs/commit-guard.md "Cross-project
authority"): they are expected behavior today, not enforcement.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys

import pytest

from tests.test_commit_guard_live import (  # noqa: F401  (world is a fixture)
    CID_A,
    _bash_guard,
    _dead_url,
    _diverge,
    _git,
    world,
)
from tests.test_hook_delivery_live import live_ats  # noqa: F401  (fixture)

CID_X = "cccccccc-0000-0000-0000-000000000003"
CID_Y = "dddddddd-0000-0000-0000-000000000004"
EDIT_TOOLS = ("Write", "Edit", "MultiEdit", "NotebookEdit")


@pytest.fixture
def cross(world):  # noqa: F811
    gov = world["gov"]
    (gov / "h.txt").write_text("h v1\n")  # untracked; Y's file
    x = world["session"]("claude-code:cccccccc", os.path.realpath(world["free"]))
    world["lock"](x, "g.txt", "advisory")  # X's own work, in its own repository
    y = world["session"]("claude-code:dddddddd", world["gov_real"])
    world["lock"](y, "h.txt", "exclusive")
    return {**world, "x": x, "y": y,
            "env_x": world["claude_env"](CID_X, x),
            "env_y": world["claude_env"](CID_Y, y)}


def _edit_guard(tool, path, env, cid):
    # Each tool's real input shape: NotebookEdit names its file notebook_path.
    key = "notebook_path" if tool == "NotebookEdit" else "file_path"
    payload = {"tool_name": tool, "session_id": cid, "tool_input": {key: str(path)}}
    return subprocess.run([sys.executable, "-m", "ai_team_sync.hooks.pre_tool_use_lockcheck"],
                          input=json.dumps(payload), env=env, capture_output=True, text=True,
                          timeout=30)


# --- direct file tools ----------------------------------------------------------

@pytest.mark.parametrize("tool", EDIT_TOOLS)
def test_file_tools_refuse_the_anchored_session_in_another_governed_repo(cross, tool):
    r = _edit_guard(tool, cross["gov"] / "new.txt", cross["env_x"], CID_X)
    assert r.returncode == 2, r.stderr
    assert "ATS CLAIM GUARD" in r.stderr and "no live lock" in r.stderr


@pytest.mark.parametrize("tool", EDIT_TOOLS)
def test_file_tools_allow_the_explicitly_switched_session(cross, tool):
    ok = _edit_guard(tool, cross["gov"] / "h.txt", cross["env_y"], CID_Y)
    assert ok.returncode == 0, ok.stderr
    refused = _edit_guard(tool, cross["gov"] / "h.txt", cross["env_x"], CID_X)
    assert refused.returncode == 2, refused.stderr


# --- Bash writes ----------------------------------------------------------------

@pytest.mark.parametrize("command", [
    "echo x > new.txt",
    "printf 'x' >> h.txt",
    "cp f.txt new.txt",
    "sed -i 's/v1/v2/' g.txt",
    "tee new.txt < /dev/null",
    "rm adv.txt",
])
def test_bash_writes_refuse_the_anchored_session(cross, command):
    r = _bash_guard(command, cross["gov"], cross["env_x"], cid=CID_X)
    assert r.returncode == 2, (command, r.stderr)
    assert "ATS" in r.stderr


def test_bash_write_allowed_for_the_explicitly_switched_session(cross):
    ok = _bash_guard("echo y > h.txt", cross["gov"], cross["env_y"], cid=CID_Y)
    assert ok.returncode == 0, ok.stderr


# --- git: commit and merge --------------------------------------------------------

def test_commit_of_another_sessions_file_is_refused_by_the_pre_commit_hook(cross):
    gov, env_x = cross["gov"], cross["env_x"]
    (gov / "f.txt").write_text("x was here\n")  # f.txt: A's exclusive lock
    _git(gov, "add", "f.txt", env=env_x)

    c = _git(gov, "commit", "-m", "x", env=env_x, check=False)
    assert c.returncode != 0 and "REFUSED (pre-commit)" in c.stderr, c.stderr


def test_bash_guard_does_not_judge_git_commit_residual(cross):
    """RESIDUAL: a commit writes no working-tree file, so the Bash guard passes
    `git commit`; the git pre-commit hook is the commit boundary, which exists
    only in repositories where the ATS git hooks are installed and is skipped
    by --no-verify."""
    gov, env_x = cross["gov"], cross["env_x"]
    (gov / "f.txt").write_text("x was here\n")
    _git(gov, "add", "f.txt", env=env_x)
    r = _bash_guard("git commit -m x", gov, env_x, cid=CID_X)
    assert r.returncode == 0, r.stderr


def test_merge_touching_another_sessions_file_is_refused(cross):
    gov = cross["gov"]
    _diverge(gov, cross["env"](None, ATS_COMMIT_GUARD_OVERRIDE="test setup"))
    r = _bash_guard("git merge --no-edit side", gov, cross["env_x"], cid=CID_X)
    assert r.returncode == 2, r.stderr
    m = _git(gov, "merge", "--no-edit", "side", env=cross["env_x"], check=False)
    assert m.returncode != 0 and "REFUSED (pre-merge-commit)" in m.stderr, m.stderr
    _git(gov, "merge", "--abort", env=cross["env_x"], check=False)


def test_commit_of_an_unclaimed_file_is_refused_for_an_identified_session(cross):
    """Codex review fc2efd12: a commit must need the committer's own claim."""
    gov, env_x = cross["gov"], cross["env_x"]
    (gov / "new.txt").write_text("unclaimed\n")
    _git(gov, "add", "new.txt", env=env_x)
    c = _git(gov, "commit", "-m", "unclaimed", env=env_x, check=False)
    assert c.returncode != 0, c.stdout + c.stderr
    assert "REFUSED (pre-commit)" in c.stderr and "no live lock" in c.stderr, c.stderr


def test_commit_allowed_for_the_session_that_holds_the_claim(cross):
    gov, env_y = cross["gov"], cross["env_y"]
    _git(gov, "add", "h.txt", env=env_y)
    c = _git(gov, "commit", "-m", "y commits its own file", env=env_y, check=False)
    assert c.returncode == 0, c.stderr


def test_unidentified_committer_rule_is_unchanged(cross):
    """A human (no ATS identity) is still refused only by EXCLUSIVE locks."""
    gov = cross["gov"]
    human = cross["env"](None)
    (gov / "new.txt").write_text("human\n")
    _git(gov, "add", "new.txt", env=human)
    c = _git(gov, "commit", "-m", "human", env=human, check=False)
    assert c.returncode == 0, c.stderr


def test_merge_of_unclaimed_incoming_files_is_refused_for_an_identified_session(cross):
    gov = cross["gov"]
    setup = cross["env"](None, ATS_COMMIT_GUARD_OVERRIDE="test setup")
    _git(gov, "checkout", "-qb", "side2", env=setup)
    (gov / "u.txt").write_text("nobody holds this\n")
    _git(gov, "add", "u.txt", env=setup)
    _git(gov, "commit", "-qm", "side2 adds u", env=setup)
    _git(gov, "checkout", "-q", "main", env=setup)
    (gov / "g.txt").write_text("main moved\n")
    _git(gov, "commit", "-qam", "main moves", env=setup)
    head = _git(gov, "rev-parse", "HEAD", env=setup).stdout.strip()

    m = _git(gov, "merge", "--no-edit", "side2", env=cross["env_x"], check=False)
    assert m.returncode != 0 and "REFUSED (pre-merge-commit)" in m.stderr, m.stderr
    assert "no live lock" in m.stderr, m.stderr
    assert _git(gov, "rev-parse", "HEAD", env=setup).stdout.strip() == head
    _git(gov, "merge", "--abort", env=setup, check=False)


# --- governed service control -----------------------------------------------------

@pytest.mark.parametrize("command", [
    "sudo systemctl restart anime-studio",
    "systemctl --user stop tower-echo-brain.service",
    "pkill -f comfyui",
])
def test_raw_service_control_of_governed_units_is_refused(cross, command):
    payload = {"tool_name": "Bash", "tool_input": {"command": command}, "session_id": CID_X}
    r = subprocess.run([sys.executable, "-m", "ai_team_sync.hooks.pre_tool_use_restart_guard"],
                       input=json.dumps(payload), env=cross["env_x"], capture_output=True,
                       text=True, timeout=30)
    assert r.returncode == 2, (command, r.stderr)
    assert "ats safe-restart" in r.stderr


# --- ATS unavailable ----------------------------------------------------------------

def test_bash_and_commit_guards_fail_closed_when_ats_is_down(cross):
    gov = cross["gov"]
    down = {**cross["env_x"], "ATS_SERVER_URL": _dead_url()}
    r = _bash_guard("echo x > new.txt", gov, down, cid=CID_X)
    assert r.returncode == 2 and "Failing closed" in r.stderr, r.stderr
    (gov / "new.txt").write_text("x\n")
    _git(gov, "add", "new.txt", env=down)
    c = _git(gov, "commit", "-m", "x", env=down, check=False)
    assert c.returncode != 0 and "fails closed" in c.stderr, c.stderr


def test_edit_guard_fails_open_when_ats_is_down_residual(cross):
    """RESIDUAL: the edit guard exits 0 when ATS cannot be reached (an outage must
    not wedge every edit). Stage 1's settings onFailure=block covers a crashed
    hook, not this deliberate exit 0."""
    down = {**cross["env_x"], "ATS_SERVER_URL": _dead_url()}
    r = _edit_guard("Write", cross["gov"] / "new.txt", down, CID_X)
    assert r.returncode == 0, r.stderr
