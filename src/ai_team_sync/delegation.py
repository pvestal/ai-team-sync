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

import os
from pathlib import Path

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


def child_state_dir(delegation_id: str) -> Path:
    """A private pointer directory for one delegated child."""
    return (Path.home() / ".local" / "share" / "ai-team-sync"
            / "delegations" / delegation_id[:8])


def child_env(base: dict[str, str], *, delegation_id: str, child_session_id: str,
              worker: str) -> dict[str, str]:
    """Environment for the delegated process. Isolation is the whole job.

    Proven live 2026-09-11: the child ran its own SessionStart, which wrote the
    SHARED pointer file, and the parent's next mutation resolved through it and
    hit the child's row. Two settings prevent the whole class:

      ATS_STATE_DIR  a private pointer directory, so nothing the child writes
                     can be read by the parent — including the legacy global file.
      ATS_SESSION_ID the row ATS already created for this delegation. It is the
                     first thing resolve_pointer consults, so the child adopts
                     that exact session instead of auto-registering a second one.

    ATS_SESSION is deliberately NOT set: session_pointer reads it as a CLAUDE
    session id (a cid), so passing an ATS session id there corrupts the key the
    per-session pointer is filed under. The first draft of the wrapper did
    exactly that.
    """
    state = child_state_dir(delegation_id)
    try:
        state.mkdir(parents=True, exist_ok=True)
    except Exception:  # noqa: BLE001 — isolation is best-effort, never fatal
        pass
    env = dict(base)
    env.pop("ATS_SESSION", None)
    env.update({
        "ATS_STATE_DIR": str(state),
        "ATS_SESSION_ID": child_session_id,
        "ATS_DELEGATION": delegation_id,
        "ATS_AGENT": f"{worker}:delegate",
    })
    return env
