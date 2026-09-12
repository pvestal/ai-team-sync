"""How a delegated child is ACTUALLY spawned, per worker.

WHY THIS MODULE EXISTS
----------------------
`cli.delegate` used to build one hardcoded command line:

    argv = ["claude", "-p", packet, *child_launch_argv(mode)]

and `child_launch_argv` returned Claude-only harness flags. So
`ats delegate --worker codex` created a child row labelled `codex:delegate`,
applied Codex's authority envelope from the worker registry, wrote Codex's
prohibitions into the packet -- and then ran the CLAUDE binary.

Proven live 2026-09-12 on delegations 82fb4676 and 5c04aa74 (ATS session
c42fec94) by process tree: `ats-mcp -> ats -> claude -p DELEGATED TASK`, with
no codex process anywhere, while `/usr/bin/codex` sat installed and unused.
Both records closed as satisfied Codex work. That is worse than having no
delegation: it manufactures provenance of an INDEPENDENT review that was
performed by the same model that asked for it.

THE TWO RULES THIS MODULE ENFORCES
----------------------------------
1. FAIL CLOSED. A worker/mode pair with no explicit enforcement mapping raises
   before anything is spawned. A mode is a safety property, not an audit label
   (see delegation.__doc__). An unmapped mode that "just runs" is an
   honour-system promise wearing a safety label, which is exactly the failure
   the mode system exists to prevent. Adding a worker to the registry without a
   spec here makes the enumeration test fail rather than producing an
   unenforced child.

2. IDENTITY COMES FROM THE SPAWN, NOT FROM A LABEL. The executable is resolved
   to an absolute path in the PARENT, before spawn, and that path is what the
   delegation record carries. It is the one identity value a child cannot
   influence: `delegation.child_env` force-sets `ATS_AGENT` to
   "<worker>:delegate", and `session_pointer.detect_agent` honours an explicit
   ATS_AGENT first, so asking the child who it is and comparing that to the
   request compares a parent-written label against itself.

ENFORCEMENT IS PER WORKER, NOT PER MODE ALONE
---------------------------------------------
The subtle trap in this repair is that swapping only the BINARY would be worse
than the bug. Claude's READ_ONLY is enforced by `--permission-mode plan
--disallowedTools ...`; hand those flags to Codex and they mean nothing, so
READ_ONLY would silently decay to a promise. Each spec therefore owns its own
enforcement for every mode it supports, and a mode it cannot enforce is a mode
it does not support.

Codex enforcement verified live 2026-09-12: under `--sandbox read-only` a write
attempt returned `patch rejected: writing is blocked by read-only sandbox` and
no file appeared. `approval_policy=never` is required because `codex exec`
otherwise blocks on an approval prompt no automated parent will answer, and
`-C <repo>` is required because Codex refuses to start outside a trusted
directory ("Not inside a trusted directory and --skip-git-repo-check was not
specified").
"""

from __future__ import annotations

import shutil
from dataclasses import dataclass, field
from typing import Callable, Mapping

from ai_team_sync.delegation import IMPLEMENT, MODES, READ_ONLY, VERIFY

# Bump when a spec's binary, prompt convention or enforcement flags change, so a
# stored delegation says which contract it was launched under. A record that
# cannot name its launcher contract cannot be audited after the contract moves.
SPEC_VERSION = "1"


class RoutingFailure(RuntimeError):
    """This delegation must NOT be spawned, and must not be recorded as work.

    Every path that raises this is a case where continuing would either run the
    wrong model under another model's name, or run the right model without the
    enforcement its mode promised.
    """


@dataclass(frozen=True)
class LaunchSpec:
    """One worker's launch contract.

    mode_args is the whole allow-list: a mode absent from it is a mode this
    worker cannot be delegated in, and `build_launch` refuses rather than
    guessing a default.
    """

    worker: str
    executable: str
    # Subcommand between the binary and its options, e.g. ("exec",) for Codex.
    subcommand: tuple[str, ...] = ()
    # The flag that carries the packet, or None when the packet is positional.
    prompt_flag: str | None = None
    # Flag that sets the working directory, when the worker needs one.
    repo_flag: str | None = None
    # Whether a spawn without a repo path is refused. Codex will not start
    # outside a trusted directory, so a repo-less Codex spawn burns its lease
    # and returns nothing; better to refuse in the parent.
    requires_repo: bool = False
    # Config prefix through which this worker's own MCP server environment must
    # be overridden, or None when the worker simply inherits the process env.
    #
    # Claude spawns its MCP children from the environment we hand subprocess.run,
    # so delegation.child_env reaches them and nothing more is needed. Codex does
    # NOT: it starts MCP servers from its own config file, and a declared
    # [mcp_servers.<name>.env] block REPLACES the inherited environment rather
    # than extending it. Observed live 2026-09-12 on delegation b5213432: the
    # child's ats-mcp never saw ATS_SESSION_ID or ATS_STATE_DIR, fell back to the
    # shared ~/.ats_session, and reported the PARENT's session as its own. The
    # mutation guard held (source 'global' is refused), so nothing was corrupted,
    # but the child could not act as itself at all.
    mcp_env_config_prefix: str | None = None
    mode_args: Mapping[str, tuple[str, ...]] = field(default_factory=dict)


# ---------------------------------------------------------------------------
# Claude READ_ONLY: a capability SPLIT, not a blanket shutdown
# ---------------------------------------------------------------------------
# Operator ruling 2026-09-12, after a Codex-led canary. READ_ONLY means the
# child cannot mutate the WORK PRODUCT or Tower Task authority state. It has
# never meant the child is expelled from the coordination plane, and conflating
# the two broke the delegated lifecycle itself.
#
# WHAT WENT WRONG. READ_ONLY was `--permission-mode plan`, which refuses EVERY
# MCP call including pure GETs. Reproduced verbatim before this change:
#
#     Cannot call mcp__ai-team-sync__my_authority while in plan mode.
#
# So the child could not read its own authority, its own session, the delegation
# state, or the decision history it was delegated to consult. Plan mode also
# writes a plan file under ~/.claude/plans/, so it was simultaneously too broad
# for coordination and not actually a no-write guarantee.
#
# Tool names are a LIE in both directions, so the split below was derived by
# inspecting each handler, never by reading a name (operator instruction):
#   * `check_locks` and `whos_editing` are POSTs whose handlers issue no DB
#     write at all -- query endpoints that happen to take a body.
#   * `delegate` issues NO HTTP and shells out to `ats delegate`: it is
#     recursive delegation, which READ_ONLY explicitly prohibits. A name-based
#     or verb-based allow-list would have let it through.

# Coordination reads a READ_ONLY child needs to do its job. Each is a GET
# against the local service; `get_tower_task` is the canonical Tower Task
# envelope in Echo Brain, the same builder the packet is rendered from.
_READ_ONLY_COORDINATION_READS = (
    "mcp__ai-team-sync__my_authority",        # authority: base, mode, effective
    "mcp__ai-team-sync__get_session_details",  # its own session
    "mcp__ai-team-sync__team_status",          # who else holds what
    "mcp__ai-team-sync__get_decision_history",  # prior rulings it must not re-litigate
    "mcp__ai-team-sync__delegation_status",    # its own delegation's state
    "mcp__ai-team-sync__ats_version",          # MCP/REST skew before any claim
    "mcp__echo-brain__get_tower_task",         # canonical Tower Task authority
)

# Built-ins a READ_ONLY child keeps. This is an ALLOW-list: `--tools` names the
# available set, so every unlisted built-in is ABSENT, not merely refused.
# Verified live -- Write, Edit, Bash and Task each answered "No such tool
# available: X. X is disabled for this session, in subagents as well as here",
# and that last clause is what closes the spawn-a-subagent-to-write escape.
_READ_ONLY_BUILTINS = "Read,Grep,Glob"

# Explicit denial of the acts the ruling names. Redundant by design: the
# allow-list plus `--permission-prompts none` already denies anything unlisted,
# so this list is a readable statement of the contract and a second lock if the
# allow-list is ever widened. It is NOT the primary mechanism, which is why a
# mutating tool nobody remembered to add here is still denied.
_READ_ONLY_DENIED = (
    # The work product.
    "Edit", "Write", "NotebookEdit",
    # Coordination-plane mutation, including another worker's session.
    "mcp__ai-team-sync__start_session", "mcp__ai-team-sync__extend_scope",
    "mcp__ai-team-sync__complete_session", "mcp__ai-team-sync__pause_session",
    "mcp__ai-team-sync__resume_session", "mcp__ai-team-sync__delete_lock",
    "mcp__ai-team-sync__log_decision", "mcp__ai-team-sync__request_override",
    "mcp__ai-team-sync__respond_to_request", "mcp__ai-team-sync__record_restart",
    # Marking its own homework, and delegating onward.
    "mcp__ai-team-sync__reconcile_delegation", "mcp__ai-team-sync__delegate",
    # Tower Task mutation, task close and gate changes.
    "mcp__echo-brain__update_tower_task", "mcp__echo-brain__create_tower_task",
    "mcp__echo-brain__reopen_tower_task", "mcp__echo-brain__rename_task_key",
    "mcp__echo-brain__review_gate",
)

_CLAUDE_READ_ONLY = (
    # Allow-list of built-ins: no shell, no writer, no subagent spawner.
    "--tools", _READ_ONLY_BUILTINS,
    # Nobody answers prompts, so anything that WOULD prompt is denied rather
    # than hanging on an approval no automated parent gives. This is the piece
    # that makes the policy fail-closed instead of honour-based.
    "--permission-prompts", "none",
    "--allowedTools", *_READ_ONLY_COORDINATION_READS,
    "--disallowedTools", *_READ_ONLY_DENIED,
)

# VERIFY is DELIBERATELY left on the pre-ruling flags. It is the one mode that
# must run tests, so it needs a shell, and the READ_ONLY allow-list above has no
# Bash by design -- a Bash command-prefix allow-list is pattern matching on a
# composable shell, which is not a boundary a safety property should rest on.
# VERIFY therefore still carries plan mode and still cannot make coordination
# reads. That is a KNOWN remaining gap, named here rather than silently widened:
# the operator scoped this repair to READ_ONLY.
_CLAUDE_VERIFY = ("--permission-mode", "plan",
                  "--disallowedTools", "Edit", "Write", "NotebookEdit")

# `-c approval_policy=never` is not a convenience: without it `codex exec`
# waits on an approval prompt, and an automated parent never answers one.
_CODEX_NON_INTERACTIVE = ("-c", "approval_policy=never")

_SPECS: dict[str, LaunchSpec] = {
    "claude-code": LaunchSpec(
        worker="claude-code",
        executable="claude",
        prompt_flag="-p",
        mode_args={
            READ_ONLY: _CLAUDE_READ_ONLY,
            VERIFY: _CLAUDE_VERIFY,
            # IMPLEMENT deliberately adds nothing: the scope lock and the
            # prohibitions list are what bound it, and the harness must stay
            # able to write inside the claimed scope.
            IMPLEMENT: (),
        },
    ),
    "codex": LaunchSpec(
        worker="codex",
        executable="codex",
        subcommand=("exec",),
        prompt_flag=None,          # `codex exec [OPTIONS] [PROMPT]`
        repo_flag="-C",
        requires_repo=True,
        # Must match the server name in ~/.codex/config.toml.
        mcp_env_config_prefix="mcp_servers.ai-team-sync.env",
        mode_args={
            READ_ONLY: ("--sandbox", "read-only") + _CODEX_NON_INTERACTIVE,
            VERIFY: ("--sandbox", "read-only") + _CODEX_NON_INTERACTIVE,
            # workspace-write, never danger-full-access: IMPLEMENT authority is
            # "edit inside the claimed scope", not "unrestricted host access".
            IMPLEMENT: ("--sandbox", "workspace-write") + _CODEX_NON_INTERACTIVE,
        },
    ),
}

# The environment a delegated child's ATS client must see to act as ITSELF.
# ATS_SESSION_ID is the row ATS already created for this delegation and is the
# first thing resolve_pointer consults; ATS_STATE_DIR keeps the child's pointer
# writes out of $HOME so they cannot be read by, or clobber, the parent's.
_ISOLATION_ENV_KEYS = ("ATS_SESSION_ID", "ATS_STATE_DIR", "ATS_AGENT",
                       "ATS_DELEGATION")


# Registered workers with NO launch spec, and why. These are authority classes
# in the worker registry, not command-line agents, so there is nothing to spawn:
# 'local' is a Tower-side model invoked through the LLM gateway, and
# 'default'/'restricted'/'unknown' are fallback buckets for labels the registry
# has never been told about. Naming them here is documentation; the refusal
# itself comes from having no entry in _SPECS.
LAUNCHERLESS_WORKERS = ("local", "default", "restricted", "unknown")


@dataclass(frozen=True)
class ResolvedLaunch:
    """What the parent decided to run, and the truth to record about it."""

    requested_worker: str
    mode: str
    resolved_binary: str      # absolute path, resolved in the parent
    argv: list[str]
    spec_version: str = SPEC_VERSION


def spec_for(worker: str) -> LaunchSpec:
    """The launch spec for `worker`, or RoutingFailure. Never a default."""
    spec = _SPECS.get((worker or "").strip())
    if spec is None:
        raise RoutingFailure(
            f"no launch specification for worker {worker!r}: refusing to spawn. "
            f"Workers with a launcher: {', '.join(sorted(_SPECS))}. "
            f"A worker without a spec is an authority class, not a runnable agent.")
    return spec


def supported_modes(worker: str) -> tuple[str, ...]:
    """Modes this worker can be delegated in. Empty for launcherless workers."""
    try:
        spec = spec_for(worker)
    except RoutingFailure:
        return ()
    return tuple(m for m in MODES if m in spec.mode_args)


def resolve_binary(spec: LaunchSpec,
                   which: Callable[[str], str | None] = shutil.which) -> str:
    """Absolute path of the worker's executable, resolved in the parent.

    Refuses rather than falling back. A missing Codex must never become a
    Claude run wearing Codex's name -- that is the whole defect.
    """
    found = which(spec.executable)
    if not found:
        raise RoutingFailure(
            f"worker {spec.worker!r} requires executable {spec.executable!r}, "
            f"which is not on PATH: refusing to spawn. There is no fallback "
            f"launcher by design.")
    return found


def validate_launchable(worker: str, mode: str, *, repo: str = "",
                        which: Callable[[str], str | None] = shutil.which
                        ) -> tuple[LaunchSpec, str]:
    """Every refusal check except the packet, so a caller can fail BEFORE it
    creates records.

    Split out from `build_launch` because the delegation packet cannot exist
    until the server has issued a delegation id and prohibitions. Without this
    the only place to discover "codex is not installed" would be after a
    delegation row and a child session already existed, leaving orphans behind
    for a spawn that never happened.
    """
    spec = spec_for(worker)

    if mode not in spec.mode_args:
        raise RoutingFailure(
            f"worker {worker!r} has no enforcement specification for mode "
            f"{mode!r}: refusing to spawn. Supported: "
            f"{', '.join(supported_modes(worker)) or '(none)'}. A mode whose "
            f"enforcement cannot be expressed for this worker is a mode this "
            f"worker does not support.")

    if spec.requires_repo and not (repo or "").strip():
        raise RoutingFailure(
            f"worker {worker!r} requires a repository path to start "
            f"(it refuses to run outside a trusted directory), and none was "
            f"given: refusing to spawn a child that would burn its lease.")

    return spec, resolve_binary(spec, which=which)


def mcp_env_argv(spec: LaunchSpec, child_env: Mapping[str, str] | None) -> list[str]:
    """Config overrides that push the ATS isolation env into the worker's OWN
    MCP server, for workers that do not simply inherit the process environment.

    Empty for Claude, which inherits. Required for Codex: without it the child's
    ats-mcp resolves the SHARED pointer and answers "who am I" with the parent's
    session (proven live on delegation b5213432), so the child cannot log a
    decision, complete its own session, or be attributed anything it did.

    Values are emitted quoted because they are parsed as TOML: an unquoted
    filesystem path is not a valid bare TOML value.
    """
    if not spec.mcp_env_config_prefix or not child_env:
        return []
    argv: list[str] = []
    for key in _ISOLATION_ENV_KEYS:
        value = (child_env.get(key) or "").strip()
        if value:
            argv += ["-c", f'{spec.mcp_env_config_prefix}.{key}="{value}"']
    return argv


def build_launch(worker: str, mode: str, prompt: str, *, repo: str = "",
                 child_env: Mapping[str, str] | None = None,
                 which: Callable[[str], str | None] = shutil.which) -> ResolvedLaunch:
    """The exact command line for this worker and mode, or RoutingFailure.

    Pure apart from `which`, so the routing rules are testable without spawning
    anything and without depending on what happens to be installed.
    """
    spec, binary = validate_launchable(worker, mode, repo=repo, which=which)
    enforcement = spec.mode_args[mode]

    argv: list[str] = [binary, *spec.subcommand]
    argv += mcp_env_argv(spec, child_env)
    if spec.prompt_flag:
        argv += [spec.prompt_flag, prompt]
    argv += list(enforcement)
    if spec.repo_flag and repo:
        argv += [spec.repo_flag, repo]
    if not spec.prompt_flag:
        argv.append(prompt)

    return ResolvedLaunch(requested_worker=worker, mode=mode,
                          resolved_binary=binary, argv=argv)


def validate_resolution(worker: str, resolved_binary: str) -> None:
    """Server-side check that a claimed worker matches the binary that ran.

    The parent resolves and reports; this re-derives the expectation from the
    registry so a record cannot claim Codex unless a Codex binary was what got
    resolved. Deliberately a basename comparison: the install path is free to
    move (/usr/bin vs /usr/local/bin vs a pipx shim), the program name is not.
    """
    spec = spec_for(worker)
    path = (resolved_binary or "").strip()
    if not path:
        raise RoutingFailure(
            f"delegation claims worker {worker!r} but records no resolved "
            f"binary: refusing. A record with no launcher cannot evidence that "
            f"any worker ran.")
    name = path.rsplit("/", 1)[-1]
    if name != spec.executable:
        raise RoutingFailure(
            f"ROUTING FAILURE: delegation requested worker {worker!r} "
            f"(executable {spec.executable!r}) but the resolved binary was "
            f"{path!r}. This is not a satisfied {worker} delegation and must "
            f"not be recorded as one.")
