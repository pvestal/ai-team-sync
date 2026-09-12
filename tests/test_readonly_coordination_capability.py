"""READ_ONLY bounds the WORK PRODUCT, not participation in coordination.

Operator ruling 2026-09-12, after a Codex-led canary. A Codex parent delegated a
READ_ONLY investigation to Claude. The child launched as the right binary, got
the full Tower Task envelope, and did useful analysis -- and then could not read
its own authority, its own session, its delegation state, or the decision
history it was sent to consult, because `--permission-mode plan` refuses EVERY
MCP call. Reproduced verbatim before the change:

    Cannot call mcp__ai-team-sync__my_authority while in plan mode.

So the implementation equated "cannot mutate the repository" with "cannot use the
coordination plane", and the delegated lifecycle could not complete. Plan mode
was ALSO not a no-write guarantee: it writes a plan file under ~/.claude/plans/.

THE POLICY THESE TESTS FREEZE
  - built-ins are an ALLOW-list (`--tools`), so an unlisted one is ABSENT, not
    merely refused -- and absent "in subagents as well as here", which is what
    closes the spawn-a-subagent-to-write escape;
  - MCP tools are allow-listed per tool, and `--permission-prompts none` denies
    anything unlisted instead of waiting on an approval nobody gives;
  - the deny-list is a second lock and a readable contract, never the primary
    mechanism, so a mutating tool nobody remembered to add is still denied.

Assertions here are about CAPABILITY, not flag order: `_Policy` answers "can this
child call X" the way the harness resolves it. A test that pinned argv positions
would pass while the policy silently inverted.

These are the STATIC half. The live half is a write canary against a real
`claude -p`, because only that proves the flags mean what the help text says.
"""

from __future__ import annotations

import pytest

from ai_team_sync.delegation import IMPLEMENT, READ_ONLY, VERIFY
from ai_team_sync.launch_spec import RoutingFailure, build_launch

PACKET = "DELEGATED TASK — mode READ_ONLY\n\nOBJECTIVE\n  read one function"
REPO = "/opt/anime-studio"
_FOUND = {"claude": "/usr/local/bin/claude", "codex": "/usr/bin/codex"}.get


class _Policy:
    """What the harness will let a child do, resolved the way the harness does.

    Built-in: available iff `--tools` names it (or `--tools` is absent, meaning
    the whole built-in set) AND it is not denied.
    MCP tool: available iff `--allowedTools` names it AND it is not denied,
    because with prompts disabled anything unlisted is denied automatically.
    """

    def __init__(self, argv: list[str]):
        self.argv = argv
        self.builtin_allowlist = self._value("--tools")
        self.allowed = set(self._variadic("--allowedTools"))
        self.denied = set(self._variadic("--disallowedTools"))
        self.prompts = self._value("--permission-prompts")
        self.permission_mode = self._value("--permission-mode")

    def _value(self, flag: str) -> str | None:
        return self.argv[self.argv.index(flag) + 1] if flag in self.argv else None

    def _variadic(self, flag: str) -> list[str]:
        if flag not in self.argv:
            return []
        out = []
        for token in self.argv[self.argv.index(flag) + 1:]:
            if token.startswith("--"):
                break
            out.append(token)
        return out

    @property
    def fails_closed(self) -> bool:
        """An unlisted tool is DENIED, not queued behind a prompt nobody answers."""
        return self.prompts == "none"

    def allows_builtin(self, tool: str) -> bool:
        if tool in self.denied:
            return False
        if self.builtin_allowlist is None:
            return True                      # no --tools: the whole built-in set
        return tool in self.builtin_allowlist.split(",")

    def allows_mcp(self, tool: str) -> bool:
        # Plan mode refuses EVERY MCP call, allow-listed or not. Measured live,
        # verbatim: "Cannot call mcp__ai-team-sync__my_authority while in plan
        # mode." Modelling this is what makes the MUST-BE-AVAILABLE tests fail
        # against the pre-ruling policy instead of passing vacuously.
        if self.permission_mode == "plan":
            return False
        if tool in self.denied:
            return False
        if tool in self.allowed:
            return True
        # Unlisted: only actually denied when nothing will answer a prompt.
        return not self.fails_closed


def claude(mode: str) -> _Policy:
    return _Policy(build_launch("claude-code", mode, PACKET, repo=REPO,
                                which=_FOUND).argv)


# ── MUST BE AVAILABLE: the coordination reads the lifecycle needs ────────────

def test_1_read_only_can_read_its_own_ats_session():
    """Requirement 1. The canary could not, and that is the defect."""
    p = claude(READ_ONLY)

    assert p.allows_mcp("mcp__ai-team-sync__get_session_details")
    assert p.allows_mcp("mcp__ai-team-sync__team_status")


def test_2_read_only_can_read_canonical_tower_task_authority():
    """Requirement 2. `get_tower_task` is Echo Brain's canonical envelope --
    the same shared builder the delegation packet is rendered from, so the child
    can re-read its binding authority rather than trusting the packet text."""
    assert claude(READ_ONLY).allows_mcp("mcp__echo-brain__get_tower_task")


def test_3_read_only_can_read_ats_decisions_and_history():
    """Requirement 3. A child that cannot read prior rulings re-litigates them."""
    p = claude(READ_ONLY)

    assert p.allows_mcp("mcp__ai-team-sync__get_decision_history")
    assert p.allows_mcp("mcp__ai-team-sync__delegation_status")


def test_read_only_can_read_its_own_authority_the_call_the_canary_lost():
    p = claude(READ_ONLY)

    assert p.allows_mcp("mcp__ai-team-sync__my_authority")
    assert p.allows_mcp("mcp__ai-team-sync__ats_version"), (
        "skew detection is a precondition for any contract claim in this repo")


def test_read_only_can_still_investigate_the_repository():
    """Bounding writes must not blind the reader."""
    p = claude(READ_ONLY)

    for tool in ("Read", "Grep", "Glob"):
        assert p.allows_builtin(tool), f"a READ_ONLY investigator needs {tool}"


# ── MUST REMAIN BLOCKED ─────────────────────────────────────────────────────

@pytest.mark.parametrize("tool", ["Edit", "Write", "NotebookEdit"])
def test_4_read_only_cannot_edit_write_or_notebookedit(tool):
    """Requirement 4. Denied AND absent from the built-in allow-list: two
    independent reasons, so widening either one alone does not open the write."""
    p = claude(READ_ONLY)

    assert not p.allows_builtin(tool)
    assert tool in p.denied
    assert tool not in (p.builtin_allowlist or "").split(",")


@pytest.mark.parametrize("tool", [
    "mcp__echo-brain__update_tower_task",
    "mcp__echo-brain__create_tower_task",
    "mcp__echo-brain__reopen_tower_task",
    "mcp__echo-brain__rename_task_key",
    "mcp__echo-brain__review_gate",
])
def test_5_read_only_cannot_mutate_the_tower_task_or_its_gate(tool):
    """Requirement 5: Tower Task mutation, task close and gate changes."""
    assert not claude(READ_ONLY).allows_mcp(tool)


@pytest.mark.parametrize("tool", [
    "mcp__ai-team-sync__complete_session",
    "mcp__ai-team-sync__pause_session",
    "mcp__ai-team-sync__resume_session",
    "mcp__ai-team-sync__reconcile_delegation",
])
def test_6_read_only_cannot_close_or_mutate_a_session_or_judge_itself(tool):
    """Requirement 6. complete_session included deliberately: child self-close
    is NOT part of the contract -- the supervisor finalizes. And a child that
    could reconcile its own delegation would be marking its own homework."""
    assert not claude(READ_ONLY).allows_mcp(tool)


@pytest.mark.parametrize("tool", [
    "mcp__ai-team-sync__start_session",
    "mcp__ai-team-sync__extend_scope",
    "mcp__ai-team-sync__request_override",
    "mcp__ai-team-sync__delete_lock",
])
def test_7_read_only_cannot_escalate_its_scope_or_authority(tool):
    """Requirement 7. extend_scope and start_session are the scope-widening
    paths; request_override and delete_lock are the ways around another
    worker's claim."""
    assert not claude(READ_ONLY).allows_mcp(tool)


def test_read_only_cannot_delegate_onward():
    """`delegate` issues no HTTP at all and shells out to `ats delegate`.

    This is the case that proves mutability must be INSPECTED, not inferred: by
    name it reads like a coordination call, and by HTTP verb it looks inert.
    READ_ONLY prohibits recursive delegation.
    """
    assert not claude(READ_ONLY).allows_mcp("mcp__ai-team-sync__delegate")


def test_read_only_has_no_shell_and_no_subagent_spawner():
    """The transitive write paths, closed by omission from the allow-list.

    Bash is how `git commit`, `git push` and `echo > file` are all reachable, so
    a Bash command-prefix allow-list would be pattern matching on a composable
    shell. Task/Agent is how a child could spawn an unrestricted writer; the
    harness reports these as disabled "in subagents as well as here".
    """
    p = claude(READ_ONLY)

    for tool in ("Bash", "Task", "Agent", "NotebookEdit", "WebFetch"):
        assert not p.allows_builtin(tool), f"{tool} must not be reachable"


def test_the_policy_fails_closed_rather_than_waiting_on_a_prompt():
    """The load-bearing flag. Without it an unlisted tool merely PROMPTS, and a
    policy whose denial depends on nobody answering is not machine-enforced."""
    p = claude(READ_ONLY)

    assert p.fails_closed
    assert not p.allows_mcp("mcp__ai-team-sync__some_tool_added_next_year")


def test_read_only_no_longer_uses_plan_mode():
    """Plan mode was the defect: too broad for coordination, and not even a
    no-write guarantee since it writes a plan file under ~/.claude/plans/."""
    assert claude(READ_ONLY).permission_mode is None


# ── What this tranche deliberately did NOT change ───────────────────────────

def test_11_codex_read_only_behaviour_is_unchanged():
    """Requirement 11. Codex enforcement is its own runtime sandbox and this
    repair does not touch it. Asserted as the exact pre-existing contract."""
    argv = build_launch("codex", READ_ONLY, PACKET, repo=REPO, which=_FOUND).argv

    assert argv[0] == "/usr/bin/codex"
    assert argv[1] == "exec"
    assert argv[argv.index("--sandbox") + 1] == "read-only"
    assert "approval_policy=never" in argv
    assert argv[argv.index("-C") + 1] == REPO
    assert argv[-1] == PACKET
    # Claude's flags must never leak into Codex, where they mean nothing.
    for flag in ("--tools", "--permission-prompts", "--allowedTools",
                 "--disallowedTools", "--permission-mode"):
        assert flag not in argv


def test_claude_verify_keeps_its_previous_enforcement_untouched():
    """VERIFY is the one mode that must run tests, so it needs a shell, and the
    READ_ONLY allow-list has none. The operator scoped this repair to READ_ONLY,
    so VERIFY keeps plan mode -- and keeps the same coordination limitation.
    That is a KNOWN remaining gap, frozen here so it cannot be forgotten or
    silently widened.
    """
    p = claude(VERIFY)

    assert p.permission_mode == "plan"
    assert p.builtin_allowlist is None
    for tool in ("Edit", "Write", "NotebookEdit"):
        assert tool in p.denied


def test_claude_implement_still_writes_inside_its_claimed_scope():
    """Repository write protection must not be weakened, and must not be
    widened either: IMPLEMENT was already unrestricted at the harness level and
    bounded by the scope lock. Unchanged."""
    argv = build_launch("claude-code", IMPLEMENT, PACKET, repo=REPO,
                        which=_FOUND).argv

    assert argv == ["/usr/local/bin/claude", "-p", PACKET]


@pytest.mark.parametrize("worker,mode", [
    ("cursor", READ_ONLY),
    ("local", READ_ONLY),
    ("local:qwen3-30b", READ_ONLY),
    ("default", READ_ONLY),
    ("restricted", READ_ONLY),
    ("unknown", IMPLEMENT),
])
def test_12_unsupported_worker_and_mode_mappings_still_fail_closed(worker, mode):
    """Requirement 12. A worker with no launch spec is an authority class, not a
    runnable agent, and must be refused rather than run unenforced."""
    with pytest.raises(RoutingFailure):
        build_launch(worker, mode, PACKET, repo=REPO, which=_FOUND)


def test_a_mode_with_no_enforcement_mapping_is_refused_not_defaulted():
    with pytest.raises(RoutingFailure):
        build_launch("claude-code", "SOMETHING_NEW", PACKET, repo=REPO,
                     which=_FOUND)
