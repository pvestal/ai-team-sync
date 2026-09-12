"""The delegated child is launched by the worker it CLAIMS to be launched by.

Regression corpus for the false-provenance defect found by the 2026-09-12
coordination canary (ATS session c42fec94): `ats delegate --worker codex`
created a row labelled `codex:delegate`, applied Codex's authority envelope,
and then ran the CLAUDE binary, because cli.py hardcoded
`argv = ["claude", "-p", packet]`. Proven live on two delegations by process
tree (ats-mcp -> ats -> claude). The record asserted an independent Codex
review that never happened.

Two properties are pinned here, and neither is a style preference:

  1. FAIL CLOSED. A worker/mode pair with no explicit enforcement mapping must
     raise before anything is spawned. A mode is a safety property; an unmapped
     mode that "just runs" is an honour-system promise wearing a safety label.

  2. IDENTITY COMES FROM THE SPAWN. The executable is resolved to an absolute
     path in the parent, and that is what gets recorded. ATS_AGENT is injected
     by the parent (delegation.child_env), so comparing a child's self-report
     against it compares a label with itself and can never detect misrouting.
"""

from __future__ import annotations

import pytest

from ai_team_sync.delegation import IMPLEMENT, MODES, READ_ONLY, VERIFY
from ai_team_sync.launch_spec import (
    RoutingFailure,
    SPEC_VERSION,
    build_launch,
    spec_for,
    supported_modes,
    validate_resolution,
)
from ai_team_sync.workers import registry

PACKET = "DELEGATED TASK — mode READ_ONLY\n\nOBJECTIVE\n  do the thing"
REPO = "/opt/tower-echo-brain"


def _which(mapping):
    """A stand-in for shutil.which, so tests never depend on what is installed."""
    return lambda name: mapping.get(name)


FOUND_BOTH = _which({"claude": "/usr/local/bin/claude", "codex": "/usr/bin/codex"})


# ── 1. the enumeration the operator asked for ────────────────────────────────

@pytest.mark.parametrize("worker", sorted(registry().names()))
@pytest.mark.parametrize("mode", MODES)
def test_every_registered_worker_and_mode_is_mapped_or_fails_closed(worker, mode):
    """Every registered worker x every mode: an enforcement spec, or a refusal.

    This is the test that makes 'fail closed' real. Adding a worker to the
    registry without a launch spec makes this FAIL rather than silently
    producing an unenforced child.
    """
    try:
        launch = build_launch(worker, mode, PACKET, repo=REPO, which=FOUND_BOTH)
    except RoutingFailure as exc:
        # An intentional refusal must say which worker and mode it refused.
        assert worker in str(exc)
        assert mode not in supported_modes(worker)
        return

    assert mode in supported_modes(worker)
    assert launch.resolved_binary.startswith("/"), "binary must be absolute"
    assert launch.argv[0] == launch.resolved_binary
    assert PACKET in launch.argv, "the child must actually receive its packet"
    assert launch.spec_version == SPEC_VERSION


def test_registry_workers_without_a_launcher_fail_closed():
    """'local', 'default', 'restricted' are authority classes, not CLIs.

    They are real rows in the worker registry, so without this they would fall
    through to whatever the launcher happened to default to.
    """
    for worker in ("local", "default", "restricted"):
        assert supported_modes(worker) == (), f"{worker} must have no launch spec"
        with pytest.raises(RoutingFailure):
            build_launch(worker, READ_ONLY, PACKET, repo=REPO, which=FOUND_BOTH)


# ── 2. Claude enforcement must not regress ───────────────────────────────────

def test_claude_verify_gets_a_shell_and_an_os_sandbox_not_plan_mode():
    """VERIFY moved off plan mode too (operator ruling 2026-09-12, second tranche).

    Plan mode was worse for VERIFY than for READ_ONLY: it blocked the coordination
    reads AND, measured across three runs, the child declined to run the
    unmodified CI command because pytest writes __pycache__. So the mode that
    exists to RUN things could not run them, and its filesystem guarantee was the
    model's own compliance.

    VERIFY now gets Bash, and containment is the OS sandbox plus a disposable
    worktree. Full coverage in tests/test_verify_mode_capability.py.
    """
    argv = build_launch("claude-code", VERIFY, PACKET, repo=REPO, which=FOUND_BOTH).argv
    assert argv[:3] == ["/usr/local/bin/claude", "-p", PACKET]

    tail = argv[3:]
    assert "--permission-mode" not in tail, "plan mode was the defect"
    assert tail[tail.index("--tools") + 1] == "Read,Grep,Glob,Bash"
    assert tail[tail.index("--permission-prompts") + 1] == "none"
    # The sandbox, and the hard gate that stops a missing backend from silently
    # running the shell unconfined.
    settings = tail[tail.index("--settings") + 1]
    assert '"enabled":true' in settings
    assert '"failIfUnavailable":true' in settings
    assert '"allowUnsandboxedCommands":false' in settings
    # Still not an implementer.
    for writer in ("Edit", "Write", "NotebookEdit"):
        assert writer in tail


def test_claude_read_only_is_at_least_as_strong_as_plan_mode_was():
    """READ_ONLY moved OFF plan mode by operator ruling 2026-09-12, because plan
    mode refused every coordination read the delegated lifecycle needs.

    Strength is asserted as a PROPERTY, not as a flag list. The literal-flag
    assertion this replaces could only ever say "identical", so it could not
    distinguish a narrowing from a weakening — and the direction is the whole
    question. Every writer plan mode blocked is still blocked; the shell and the
    subagent spawner plan mode ALLOWED are now gone as well, so the write surface
    is strictly smaller. Full coverage in
    tests/test_readonly_coordination_capability.py.
    """
    argv = build_launch("claude-code", READ_ONLY, PACKET, repo=REPO,
                        which=FOUND_BOTH).argv
    assert argv[:3] == ["/usr/local/bin/claude", "-p", PACKET]

    tail = argv[3:]
    # Still denied, exactly as plan mode denied them.
    for writer in ("Edit", "Write", "NotebookEdit"):
        assert writer in tail
    # Built-ins are now an allow-list, so the shell and the subagent spawner are
    # absent rather than merely discouraged.
    assert tail[tail.index("--tools") + 1] == "Read,Grep,Glob"
    # And nothing unlisted can slip through on an unanswered prompt.
    assert tail[tail.index("--permission-prompts") + 1] == "none"


def test_claude_implement_mode_keeps_write_access():
    argv = build_launch("claude-code", IMPLEMENT, PACKET, repo=REPO, which=FOUND_BOTH).argv
    assert argv == ["/usr/local/bin/claude", "-p", PACKET]


# ── 3. Codex is actually Codex, and actually sandboxed ───────────────────────

def test_codex_read_only_uses_a_real_sandbox_not_an_instruction():
    """`-s read-only` is enforced by Codex itself, verified live 2026-09-12:
    a write attempt returned 'patch rejected: writing is blocked by read-only
    sandbox'. A prompt that merely ASKS the model not to write is not this."""
    launch = build_launch("codex", READ_ONLY, PACKET, repo=REPO, which=FOUND_BOTH)
    assert launch.argv[0] == "/usr/bin/codex"
    assert launch.argv[1] == "exec"
    assert "--sandbox" in launch.argv
    assert launch.argv[launch.argv.index("--sandbox") + 1] == "read-only"
    # Non-interactive: without this Codex waits for an approval nobody will give.
    assert "approval_policy=never" in launch.argv
    # Codex refuses to run outside a trusted directory; the repo must be passed.
    assert "-C" in launch.argv
    assert launch.argv[launch.argv.index("-C") + 1] == REPO
    # The packet is positional for `codex exec`, and must be last.
    assert launch.argv[-1] == PACKET


def test_codex_verify_mode_is_also_read_only():
    argv = build_launch("codex", VERIFY, PACKET, repo=REPO, which=FOUND_BOTH).argv
    assert argv[argv.index("--sandbox") + 1] == "read-only"


def test_codex_implement_mode_gets_workspace_write_not_full_access():
    argv = build_launch("codex", IMPLEMENT, PACKET, repo=REPO, which=FOUND_BOTH).argv
    assert argv[argv.index("--sandbox") + 1] == "workspace-write"
    assert "danger-full-access" not in argv
    assert "--dangerously-bypass-approvals-and-sandbox" not in argv


def test_no_worker_ever_resolves_to_another_workers_binary():
    """The defect itself, as a test."""
    for worker, expected in (("codex", "/usr/bin/codex"),
                             ("claude-code", "/usr/local/bin/claude")):
        for mode in supported_modes(worker):
            launch = build_launch(worker, mode, PACKET, repo=REPO, which=FOUND_BOTH)
            assert launch.resolved_binary == expected
            assert launch.argv[0] == expected


# ── 4. refusals ──────────────────────────────────────────────────────────────

def test_unknown_worker_fails_closed():
    with pytest.raises(RoutingFailure) as exc:
        build_launch("cursor", READ_ONLY, PACKET, repo=REPO, which=FOUND_BOTH)
    assert "cursor" in str(exc.value)


def test_missing_executable_fails_closed_before_spawn():
    """Codex requested but not installed must refuse, never fall back."""
    with pytest.raises(RoutingFailure) as exc:
        build_launch("codex", READ_ONLY, PACKET, repo=REPO,
                     which=_which({"claude": "/usr/local/bin/claude"}))
    assert "codex" in str(exc.value)
    assert "claude" not in str(exc.value).replace("claude-code", "")


def test_unsupported_mode_for_a_known_worker_fails_closed():
    with pytest.raises(RoutingFailure):
        build_launch("codex", "YOLO", PACKET, repo=REPO, which=FOUND_BOTH)


def test_codex_without_a_repo_fails_closed():
    """Codex refuses outside a trusted directory, so a repo-less spawn would
    burn a lease and return nothing. Observed live 2026-09-12: 'Not inside a
    trusted directory and --skip-git-repo-check was not specified.'"""
    with pytest.raises(RoutingFailure):
        build_launch("codex", READ_ONLY, PACKET, repo="", which=FOUND_BOTH)


# ── 5. the server-side check ─────────────────────────────────────────────────

def test_validate_resolution_accepts_a_matching_binary():
    validate_resolution("codex", "/usr/bin/codex")
    validate_resolution("claude-code", "/usr/local/bin/claude")


def test_validate_resolution_rejects_the_exact_observed_defect():
    """requested=codex, resolved=claude. This is what actually happened on
    delegations 82fb4676 and 5c04aa74 and was recorded as a Codex review."""
    with pytest.raises(RoutingFailure) as exc:
        validate_resolution("codex", "/usr/local/bin/claude")
    assert "codex" in str(exc.value) and "claude" in str(exc.value)


def test_validate_resolution_rejects_an_empty_binary():
    """A record with no resolved binary cannot claim any worker ran."""
    with pytest.raises(RoutingFailure):
        validate_resolution("codex", "")


def test_spec_for_unknown_worker_raises_rather_than_defaulting():
    with pytest.raises(RoutingFailure):
        spec_for("nobody")


# ── 6. the child must be able to act as ITSELF ───────────────────────────────

CHILD_ENV = {
    "ATS_SESSION_ID": "b328ec1d-0665-43c5-8a22-1141d037c512",
    "ATS_STATE_DIR": "/home/patrick/.local/share/ai-team-sync/delegations/b52134",
    "ATS_AGENT": "codex:delegate",
    "ATS_DELEGATION": "b5213432-0bf2-45d2-b2be-b15ea2c0a882",
    "PATH": "/usr/bin",          # must NOT be forwarded
    "HOME": "/home/patrick",     # must NOT be forwarded
}


def test_codex_carries_the_isolation_env_into_its_own_mcp_server():
    """Codex starts MCP servers from its config, and a declared
    [mcp_servers.<name>.env] block REPLACES the inherited environment. Observed
    live on delegation b5213432: the child's ats-mcp never saw ATS_SESSION_ID,
    fell back to the shared ~/.ats_session and reported the PARENT's session as
    its own, so it could not log a decision or complete its own session."""
    argv = build_launch("codex", READ_ONLY, PACKET, repo=REPO,
                        child_env=CHILD_ENV, which=FOUND_BOTH).argv
    joined = " ".join(argv)
    for key in ("ATS_SESSION_ID", "ATS_STATE_DIR", "ATS_AGENT", "ATS_DELEGATION"):
        assert f"mcp_servers.ai-team-sync.env.{key}=" in joined, f"{key} not forwarded"
    assert f'mcp_servers.ai-team-sync.env.ATS_SESSION_ID="{CHILD_ENV["ATS_SESSION_ID"]}"' in joined
    # Values are parsed as TOML, so a bare filesystem path would not parse.
    assert f'.ATS_STATE_DIR="{CHILD_ENV["ATS_STATE_DIR"]}"' in joined


def test_only_the_isolation_keys_are_forwarded():
    """The override carries identity, not the parent's whole environment."""
    argv = build_launch("codex", READ_ONLY, PACKET, repo=REPO,
                        child_env=CHILD_ENV, which=FOUND_BOTH).argv
    joined = " ".join(argv)
    assert "PATH=" not in joined
    assert "HOME=" not in joined


def test_the_packet_stays_last_even_with_env_overrides():
    """`codex exec [OPTIONS] [PROMPT]`: an option appended after the positional
    would be swallowed as part of the prompt."""
    argv = build_launch("codex", READ_ONLY, PACKET, repo=REPO,
                        child_env=CHILD_ENV, which=FOUND_BOTH).argv
    assert argv[-1] == PACKET
    assert argv[-2] == REPO and argv[-3] == "-C"


def test_claude_needs_no_env_overrides_because_it_inherits():
    """Claude's MCP children are spawned from the env we hand subprocess.run,
    so delegation.child_env already reaches them. Adding flags here would be
    inventing a mechanism Claude does not have."""
    argv = build_launch("claude-code", READ_ONLY, PACKET, repo=REPO,
                        child_env=CHILD_ENV, which=FOUND_BOTH).argv
    assert "-c" not in argv
    assert "ATS_SESSION_ID" not in " ".join(argv)
    assert argv[:3] == ["/usr/local/bin/claude", "-p", PACKET]


def test_no_child_env_is_not_a_crash():
    """Callers that spawn nothing (dry-run, tooling) still build an argv."""
    argv = build_launch("codex", READ_ONLY, PACKET, repo=REPO,
                        child_env=None, which=FOUND_BOTH).argv
    assert argv[-1] == PACKET
    assert "mcp_servers" not in " ".join(argv)
