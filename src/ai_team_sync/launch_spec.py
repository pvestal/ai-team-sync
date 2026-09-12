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
    mode_args: Mapping[str, tuple[str, ...]] = field(default_factory=dict)


# Claude's flags are carried over EXACTLY as `child_launch_argv` emitted them.
# They are what made READ_ONLY real before this module existed, and the refactor
# is only allowed to add workers, never to weaken the one that already worked.
# Tool names must match the harness's own registry: an unknown name is reported
# as "matches no known tool" and silently denies NOTHING.
_CLAUDE_READ_ONLY = ("--permission-mode", "plan",
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
            VERIFY: _CLAUDE_READ_ONLY,
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
        mode_args={
            READ_ONLY: ("--sandbox", "read-only") + _CODEX_NON_INTERACTIVE,
            VERIFY: ("--sandbox", "read-only") + _CODEX_NON_INTERACTIVE,
            # workspace-write, never danger-full-access: IMPLEMENT authority is
            # "edit inside the claimed scope", not "unrestricted host access".
            IMPLEMENT: ("--sandbox", "workspace-write") + _CODEX_NON_INTERACTIVE,
        },
    ),
}

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


def build_launch(worker: str, mode: str, prompt: str, *, repo: str = "",
                 which: Callable[[str], str | None] = shutil.which) -> ResolvedLaunch:
    """The exact command line for this worker and mode, or RoutingFailure.

    Pure apart from `which`, so the routing rules are testable without spawning
    anything and without depending on what happens to be installed.
    """
    spec, binary = validate_launchable(worker, mode, repo=repo, which=which)
    enforcement = spec.mode_args[mode]

    argv: list[str] = [binary, *spec.subcommand]
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
