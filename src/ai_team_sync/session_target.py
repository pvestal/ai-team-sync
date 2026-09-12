"""WHICH session a lifecycle call mutates, decided explicitly rather than inferred.

THE PROBLEM
-----------
`complete_session` took only a summary. It resolved its target through the
ambient pointer, and its success message never named the row it closed. On
2026-09-11 that completed the WRONG session: a delegated Claude's SessionStart
overwrote the shared ~/.ats_session, a Codex parent (no cid, so no per-session
pointer) resolved through it, and its completion landed on the child's row while
reporting success.

The pointer layer was then hardened — per-session files, private state dirs for
delegated children, and `mutation_refusal` failing closed on the shared source —
and that held: two Codex delegations in this session ran without either touching
the parent's pointer. But pointer correctness is still inference. A caller that
knows which session it means should be able to SAY so, and be refused if the
system disagrees.

THE RULE
--------
    Explicit session identity wins, but any conflicting pointer that CLAIMS TO
    REPRESENT THIS CALLER must be surfaced and validated, never silently
    substituted.

Which pointers claim to represent the caller is the whole subtlety, and getting
it wrong would break the isolation that was just proven:

    in_process   this MCP process started that session. Nothing else can have
                 written it. Represents the caller.
    env          $ATS_SESSION_ID, set by whoever launched this process — which,
                 for a delegated child, is ATS naming that child's own row.
                 Represents the caller.
    per_session  ~/.ats_session_<cid8>, keyed to THIS agent session. Represents
                 the caller.
    global       ~/.ats_session — SHARED. Names whichever session wrote it last,
                 across every agent on the box. Does NOT represent the caller.

That last line is why a delegated Codex child completing itself while the global
file still names its Claude parent is NOT a conflict: the global pointer never
spoke for the child. Treating it as a conflict would refuse exactly the case the
isolation work exists to support.

A conflicting CALLER pointer is only fatal when the session it names is still
LIVE. A stale pointer left by a finished session is noise, not a competing
claim, so the explicit id wins and the discrepancy is reported rather than
raised.
"""

from __future__ import annotations

from dataclasses import dataclass

# Sources that speak for THIS caller. A disagreement here is a real conflict.
CALLER_POINTER_SOURCES = frozenset({"in_process", "env", "per_session"})

# Shared state. Names whoever wrote last; never evidence about this caller.
SHARED_POINTER_SOURCES = frozenset({"global"})


@dataclass(frozen=True)
class TargetResolution:
    """Which session to mutate, how that was decided, and what disagreed."""

    session_id: str | None
    # "explicit" when the caller named it; otherwise the pointer source used.
    source: str
    # Non-fatal discrepancy worth reporting in the result.
    conflict: str | None = None
    # Non-None means DO NOT PROCEED; the text is the reason.
    refusal: str | None = None

    @property
    def ok(self) -> bool:
        return self.refusal is None and bool(self.session_id)


def resolve_completion_target(*, explicit_id: str | None,
                              pointer_id: str | None,
                              pointer_source: str,
                              pointer_names_live_session: bool | None = None
                              ) -> TargetResolution:
    """Decide the target. Pure, so the policy is testable without a server.

    `pointer_names_live_session` is the caller's lookup of whether the pointer's
    session is still active. None means "could not be established", which is
    treated as LIVE: a conflict we cannot disprove must not be waved through.
    """
    explicit = (explicit_id or "").strip()
    pointer = (pointer_id or "").strip()

    if explicit:
        if pointer and pointer != explicit:
            if pointer_source in CALLER_POINTER_SOURCES:
                if pointer_names_live_session is False:
                    return TargetResolution(
                        explicit, "explicit",
                        conflict=(f"this process's {pointer_source} pointer names "
                                  f"{pointer}, which is no longer active; completing "
                                  f"the session you named ({explicit}) instead."))
                return TargetResolution(
                    None, "explicit",
                    refusal=(
                        f"Refusing to complete {explicit}: this process's "
                        f"{pointer_source} pointer names a DIFFERENT live session "
                        f"({pointer}). One of the two is wrong and guessing which "
                        f"is how the 2026-09-11 wrong-session completion happened. "
                        f"Complete {pointer}, or clear/point the pointer at "
                        f"{explicit}, then retry."))
            # A shared pointer does not speak for this caller. Report, never refuse:
            # this is exactly a delegated child completing itself while the global
            # file still names its parent.
            return TargetResolution(
                explicit, "explicit",
                conflict=(f"the SHARED pointer names {pointer}, which is not "
                          f"evidence about this caller; completing the session you "
                          f"named ({explicit})."))
        return TargetResolution(explicit, "explicit")

    # ── legacy path: no id supplied, resolve through the guarded pointer ──
    if not pointer:
        return TargetResolution(
            None, pointer_source or "none",
            refusal=("No session id was given and no pointer names one for this "
                     "process. Pass session_id explicitly, start a session, or set "
                     "ATS_SESSION_ID; refusing to guess which session to complete."))

    if pointer_source in SHARED_POINTER_SOURCES:
        return TargetResolution(
            None, pointer_source,
            refusal=(f"Refusing to complete {pointer}: that id came from the SHARED "
                     f"pointer file, which names whichever session wrote it last and "
                     f"not necessarily yours. Pass session_id explicitly, or start a "
                     f"session in this process."))

    return TargetResolution(pointer, pointer_source)


def ownership_refusal(target_id: str, row: dict | None, my_label: str,
                      *, delegation_child_id: str | None = None) -> str | None:
    """Why this caller may NOT complete `target_id`, or None to allow.

    Completing a session is an act on somebody's record, so the bar is identity,
    not proximity: a parent may not close its child's session and a child may not
    close its parent's. The delegation binding is the one exception, and it is
    exact — ATS created that child row for this delegation, which no label
    comparison can improve on.

    Fails closed: a row that cannot be read cannot be shown to be ours.
    """
    if delegation_child_id and delegation_child_id == target_id:
        return None

    if row is None:
        return (f"Refusing to complete {target_id}: that session was not found on "
                f"the server, so ownership cannot be established.")

    agent = str(row.get("agent") or "")
    if agent and my_label and agent != my_label:
        return (f"Refusing to complete {target_id}: it belongs to {agent}, not to "
                f"{my_label}. A session is completed by the worker that owns it — "
                f"a parent does not close its child's record, and a child does not "
                f"close its parent's.")
    return None
