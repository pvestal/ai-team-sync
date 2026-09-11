"""Delegation: a bounded subproblem handed from one worker to another.

DELEGATION IS NOT HANDOFF. If Codex owns a task and delegates a subproblem to
Claude, Codex still owns the task while Claude works and after Claude returns.
Ownership moves only by an explicit handoff, never as a side effect of a child
finishing. Everything here exists to keep that true when nobody is watching.

A mode is a SAFETY PROPERTY, not an audit label. READ_ONLY that merely decorated
a record would be a note attached to a worker which can still edit six files and
fire the GPU, and the operator would have been told it was read-only.

Authority under a delegation is an INTERSECTION, never a promotion: the mode can
only narrow what the worker registry already granted. Otherwise delegation
becomes the escalation path around the registry.
"""

from __future__ import annotations

from ai_team_sync.workers import Authority, Worker

READ_ONLY = "READ_ONLY"
IMPLEMENT = "IMPLEMENT"
VERIFY = "VERIFY"
MODES = (READ_ONLY, IMPLEMENT, VERIFY)

# What each mode FORBIDS. Written as prohibitions rather than permissions so a
# new capability is denied until somebody decides it is allowed, and so the
# record a human reads says plainly what the child could not do.
_PROHIBITIONS: dict[str, tuple[str, ...]] = {
    READ_ONLY: (
        "file_write", "git_commit", "service_restart", "gpu_submit",
        "task_state_mutation", "parent_task_close", "recursive_delegation",
    ),
    IMPLEMENT: (
        # Writes are allowed INSIDE the declared scope only; the scope lock is
        # what makes that checkable. GPU and service actions stay separately
        # gated — implementing a fix is not authority to spend a render.
        "service_restart", "gpu_submit", "parent_task_close",
        "recursive_delegation",
    ),
    VERIFY: (
        "file_write", "git_commit", "service_restart", "gpu_submit",
        "parent_task_close", "recursive_delegation",
    ),
}

_MODE_AUTHORITY: dict[str, Authority] = {
    READ_ONLY: Authority(edit="none", commit=False, task_close="no"),
    IMPLEMENT: Authority(edit="claimed_scope", commit=True, task_close="no"),
    VERIFY: Authority(edit="none", commit=False, task_close="no"),
}


def prohibitions_for(mode: str) -> list[str]:
    return list(_PROHIBITIONS.get(mode, _PROHIBITIONS[READ_ONLY]))


def effective_authority(worker: Worker, mode: str) -> Authority:
    """min(worker authority, mode authority) — narrowing only."""
    mode_auth = _MODE_AUTHORITY.get(mode, _MODE_AUTHORITY[READ_ONLY])
    edit = "claimed_scope" if (worker.authority.edit == "claimed_scope"
                               and mode_auth.edit == "claimed_scope") else "none"
    close_rank = {"no": 0, "conditional": 1, "yes": 2}
    task_close = min(worker.authority.task_close, mode_auth.task_close,
                     key=lambda v: close_rank.get(v, 0))
    return Authority(edit=edit,
                     commit=bool(worker.authority.commit and mode_auth.commit),
                     task_close=task_close)


def child_launch_argv(mode: str) -> list[str]:
    """Harness-level enforcement for a delegated Claude Code child.

    The ATS record refuses the CLAIM; this refuses the ACT. `plan` mode is the
    harness's own read-only posture, and the disallowed list closes the obvious
    write paths so 'READ_ONLY' is not honour-system. Kept next to the
    prohibitions it implements so the two cannot drift apart silently.
    """
    if mode == IMPLEMENT:
        return []
    return ["--permission-mode", "plan",
            "--disallowedTools", "Edit", "Write", "MultiEdit", "NotebookEdit"]
