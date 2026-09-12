"""VERIFY is verification-capable, non-authoritative, non-implementation.

Operator ruling 2026-09-12, second coordination tranche. READ_ONLY was repaired in
d685142; VERIFY was deliberately left on plan mode and named as a known gap. This
closes it.

WHAT PLAN MODE ACTUALLY DID TO VERIFY, measured before any change:
  * it refused every ATS and Echo Brain call, verbatim
    "Cannot call mcp__ai-team-sync__my_authority while in plan mode";
  * and across THREE independent runs the child declined the shell writes ITSELF
    rather than the harness denying them, including declining to run the
    unmodified CI command `python3 -m pytest -q` because pytest writes
    __pycache__.

So the mode whose entire purpose is to RUN verification could not run it, and the
filesystem guarantee it appeared to provide was the model's own compliance. Both
halves are defects, and the second is why containment here is environmental.

THE HONEST MODEL. VERIFY gets a real shell. A shell can write files, so no tool
list makes VERIFY filesystem-read-only and this file does not pretend otherwise.
What bounds it is Claude Code's Bash sandbox (bubblewrap + seccomp, declared with
failIfUnavailable so a missing backend refuses rather than running unconfined)
plus a disposable worktree as the writable cwd. The worktree half is tested in
tests/test_verify_worktree_containment.py; the kernel half is proven by live
canary, because only a real run can show "Read-only file system".

Capability assertions reuse `_Policy` from the READ_ONLY tranche on purpose: one
model of how the harness resolves a tool, so the two modes cannot be described by
two different theories.
"""

from __future__ import annotations

import pytest

from ai_team_sync.delegation import IMPLEMENT, READ_ONLY, VERIFY, effective_authority
from ai_team_sync.launch_spec import RoutingFailure, build_launch
from ai_team_sync.workers import registry
from tests.test_readonly_coordination_capability import PACKET, REPO, _FOUND, _Policy, claude


def verify() -> _Policy:
    return claude(VERIFY)


# ── 1. READ_ONLY from d685142 must not regress ──────────────────────────────

def test_1_read_only_policy_is_byte_for_byte_what_d685142_shipped():
    """Requirement 1. Pinned as the exact token sequence, because 'similar' is
    what a regression looks like from the inside."""
    argv = build_launch("claude-code", READ_ONLY, PACKET, repo=REPO, which=_FOUND).argv

    assert argv[3:] == [
        "--tools", "Read,Grep,Glob",
        "--permission-prompts", "none",
        "--allowedTools",
        "mcp__ai-team-sync__my_authority",
        "mcp__ai-team-sync__get_session_details",
        "mcp__ai-team-sync__team_status",
        "mcp__ai-team-sync__get_decision_history",
        "mcp__ai-team-sync__delegation_status",
        "mcp__ai-team-sync__ats_version",
        "mcp__echo-brain__get_tower_task",
        "--disallowedTools",
        "Edit", "Write", "NotebookEdit",
        "mcp__ai-team-sync__start_session", "mcp__ai-team-sync__extend_scope",
        "mcp__ai-team-sync__complete_session", "mcp__ai-team-sync__pause_session",
        "mcp__ai-team-sync__resume_session", "mcp__ai-team-sync__delete_lock",
        "mcp__ai-team-sync__log_decision", "mcp__ai-team-sync__request_override",
        "mcp__ai-team-sync__respond_to_request", "mcp__ai-team-sync__record_restart",
        "mcp__ai-team-sync__reconcile_delegation", "mcp__ai-team-sync__delegate",
        "mcp__echo-brain__update_tower_task", "mcp__echo-brain__create_tower_task",
        "mcp__echo-brain__reopen_tower_task", "mcp__echo-brain__rename_task_key",
        "mcp__echo-brain__review_gate",
    ]


def test_1_read_only_still_has_no_shell():
    """The one thing VERIFY changes must not bleed into READ_ONLY."""
    p = claude(READ_ONLY)

    for tool in ("Bash", "Task", "Agent", "Edit", "Write", "NotebookEdit"):
        assert not p.allows_builtin(tool)


def test_1_read_only_carries_no_sandbox_settings_it_does_not_need():
    """READ_ONLY has no shell, so the sandbox is not part of its contract.
    Adding it there would be cargo-culting this tranche's mechanism."""
    argv = build_launch("claude-code", READ_ONLY, PACKET, repo=REPO, which=_FOUND).argv

    assert "--settings" not in argv


# ── 2. the right executable ─────────────────────────────────────────────────

def test_2_verify_launches_the_claude_executable():
    launch = build_launch("claude-code", VERIFY, PACKET, repo=REPO, which=_FOUND)

    assert launch.resolved_binary == "/usr/local/bin/claude"
    assert launch.argv[0] == "/usr/local/bin/claude"
    assert launch.argv[1] == "-p"
    assert launch.argv[2] == PACKET


# ── 3, 4, 5. the coordination reads VERIFY must have ────────────────────────

def test_3_verify_can_read_its_exact_ats_authority_and_session():
    """Requirement 3. `my_authority` with a session_id is the call that returns
    the NARROWED row; get_session_details is how it reads its own record."""
    p = verify()

    assert p.allows_mcp("mcp__ai-team-sync__my_authority")
    assert p.allows_mcp("mcp__ai-team-sync__get_session_details")
    assert p.allows_mcp("mcp__ai-team-sync__delegation_status")


def test_4_verify_can_read_canonical_tower_task_authority():
    """Requirement 4. A reviewer that cannot read the acceptance criteria is
    reviewing against its own reconstruction of them."""
    assert verify().allows_mcp("mcp__echo-brain__get_tower_task")


def test_5_verify_can_read_ats_history_and_decisions():
    p = verify()

    assert p.allows_mcp("mcp__ai-team-sync__get_decision_history")
    assert p.allows_mcp("mcp__ai-team-sync__team_status")
    assert p.allows_mcp("mcp__ai-team-sync__ats_version")


def test_the_coordination_read_set_is_identical_to_read_only():
    """One answer to "what may a delegated child read", not two that drift."""
    assert verify().allowed - {"Bash"} == claude(READ_ONLY).allowed


# ── 6, 7. verification capability ───────────────────────────────────────────

def test_6_verify_has_a_real_shell_so_it_can_run_tests():
    """Requirement 6. Granted, not pattern-matched: the containment is the
    sandbox, so there is no reason to also cripple the command line."""
    p = verify()

    assert p.allows_builtin("Bash")
    assert "Bash" in p.allowed, "granted explicitly, since prompts are disabled"


def test_7_verify_can_inspect_the_actual_result_under_review():
    """Requirement 7. `git diff`/`status`/`log` arrive through the shell, and the
    source state itself is supplied by the disposable worktree (containment
    tests). Read/Grep/Glob cover inspection that needs no shell."""
    p = verify()

    for tool in ("Read", "Grep", "Glob", "Bash"):
        assert p.allows_builtin(tool)


def test_the_sandbox_is_declared_with_a_hard_gate_not_best_effort():
    """failIfUnavailable is the load-bearing flag. Without it a host lacking a
    sandbox backend runs the shell UNCONFINED, which is the silent decay from a
    safety property into a promise that this mode system exists to prevent."""
    argv = build_launch("claude-code", VERIFY, PACKET, repo=REPO, which=_FOUND).argv
    settings = argv[argv.index("--settings") + 1]

    assert '"enabled":true' in settings
    assert '"failIfUnavailable":true' in settings
    assert '"allowUnsandboxedCommands":false' in settings, (
        "the dangerouslyDisableSandbox escape hatch must be refused")


# ── 8, 9, 10. what VERIFY must not be able to do ───────────────────────────

@pytest.mark.parametrize("tool", [
    "mcp__echo-brain__update_tower_task",
    "mcp__echo-brain__create_tower_task",
    "mcp__echo-brain__reopen_tower_task",
    "mcp__echo-brain__rename_task_key",
    "mcp__echo-brain__review_gate",
])
def test_8_verify_cannot_mutate_a_tower_task_or_change_a_gate(tool):
    """Requirement 8: no Tower mutation, no task close, no gate change, and no
    overwriting an operator decision."""
    assert not verify().allows_mcp(tool)


@pytest.mark.parametrize("tool", [
    "mcp__ai-team-sync__start_session", "mcp__ai-team-sync__extend_scope",
    "mcp__ai-team-sync__complete_session", "mcp__ai-team-sync__pause_session",
    "mcp__ai-team-sync__resume_session", "mcp__ai-team-sync__delete_lock",
    "mcp__ai-team-sync__request_override", "mcp__ai-team-sync__respond_to_request",
    "mcp__ai-team-sync__log_decision", "mcp__ai-team-sync__record_restart",
])
def test_8_verify_cannot_mutate_ats_authority_scope_or_another_session(tool):
    assert not verify().allows_mcp(tool)


def test_8_verify_cannot_reconcile_or_accept_its_own_delegation():
    """A reviewer that can accept its own review is marking its own homework."""
    assert not verify().allows_mcp("mcp__ai-team-sync__reconcile_delegation")


def test_9_onward_delegation_and_the_subagent_escape_stay_unavailable():
    """Requirement 9. `delegate` shells out to `ats delegate`, and Task/Agent
    would spawn a worker with its own policy. Neither is justified for a
    reviewer, so neither is available."""
    p = verify()

    assert not p.allows_mcp("mcp__ai-team-sync__delegate")
    assert not p.allows_builtin("Task")
    assert not p.allows_builtin("Agent")


def test_10_verify_cannot_implement_through_an_agent_tool():
    """Requirement 10, the tool half. Commit/push/deploy through the SHELL is a
    containment question, not a tool question, and is covered by the worktree
    tests plus the live canary: a commit in a detached disposable worktree
    advances no branch, and push needs a network the sandbox denies."""
    p = verify()

    for writer in ("Edit", "Write", "NotebookEdit"):
        assert not p.allows_builtin(writer)
        assert writer in p.denied


def test_the_policy_still_fails_closed_for_a_tool_nobody_listed():
    p = verify()

    assert p.fails_closed
    assert not p.allows_mcp("mcp__echo-brain__some_mutator_added_next_year")


def test_verify_does_not_hold_authority_it_was_never_granted():
    """The registry half, independent of the harness flags: VERIFY's effective
    authority is read-only and closes nothing, whichever frontier worker runs it."""
    for worker in ("claude-code", "codex"):
        auth = effective_authority(registry().resolve(worker), VERIFY)
        assert auth.edit == "none"
        assert auth.commit is False
        assert auth.task_close == "no"


# ── 15, 16. what this tranche must not change ──────────────────────────────

def test_15_codex_read_only_behaviour_is_unchanged():
    """Requirement 15."""
    argv = build_launch("codex", READ_ONLY, PACKET, repo=REPO, which=_FOUND).argv

    assert argv[0] == "/usr/bin/codex"
    assert argv[1] == "exec"
    assert argv[argv.index("--sandbox") + 1] == "read-only"
    assert "approval_policy=never" in argv
    assert argv[argv.index("-C") + 1] == REPO
    assert argv[-1] == PACKET
    for flag in ("--tools", "--permission-prompts", "--allowedTools",
                 "--disallowedTools", "--settings", "--permission-mode"):
        assert flag not in argv, f"Claude's {flag} must never reach Codex"


def test_codex_verify_is_unchanged_and_keeps_its_own_runtime_sandbox():
    """Codex VERIFY is enforced by Codex, so it gets no Claude settings and no
    verification worktree. This repair is not allowed to move it."""
    argv = build_launch("codex", VERIFY, PACKET, repo=REPO, which=_FOUND).argv

    assert argv[argv.index("--sandbox") + 1] == "read-only"
    assert "--settings" not in argv


def test_claude_implement_is_unchanged_and_still_writes_in_its_scope():
    argv = build_launch("claude-code", IMPLEMENT, PACKET, repo=REPO, which=_FOUND).argv

    assert argv == ["/usr/local/bin/claude", "-p", PACKET]


@pytest.mark.parametrize("worker,mode", [
    ("cursor", VERIFY), ("local", VERIFY), ("local:qwen3-30b", VERIFY),
    ("default", VERIFY), ("restricted", VERIFY), ("unknown", VERIFY),
])
def test_16_unsupported_worker_and_mode_combinations_stay_fail_closed(worker, mode):
    """Requirement 16."""
    with pytest.raises(RoutingFailure):
        build_launch(worker, mode, PACKET, repo=REPO, which=_FOUND)


def test_16_a_mode_with_no_mapping_is_still_refused_rather_than_defaulted():
    with pytest.raises(RoutingFailure):
        build_launch("claude-code", "AUDIT", PACKET, repo=REPO, which=_FOUND)
