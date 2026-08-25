"""Single source of truth for the SessionStart auto-registration marker.

WHY THIS MODULE EXISTS
----------------------
The SessionStart hook registers every Claude session with a placeholder
description. Two pieces of logic key off that description:

  1. orphan adoption -- when a session calls start_session for real, the
     placeholder row it left behind should be completed, not left on the board.
  2. placeholder replacement -- when a session takes real locks via
     extend_scope, its description must stop reading as an orphan.

Both were written as an exact string equality against

    "auto-registered on SessionStart"

while the hook actually wrote

    "auto-registered on SessionStart (UNSCOPED - claims nothing; extend scope /
     take locks to claim files)"

so neither predicate could ever be true. The guards were present, shipped, and
unsatisfiable. Observed 2026-08-24: agent claude-code:7bb1e161 held TWO active
sessions simultaneously -- cf21607b (placeholder, never adopted) and 4215bbc3
(the real one) -- and team_status counted both, reporting 9 active sessions on a
board with roughly one live worker.

That is the failure this module prevents. The marker text and the predicate that
recognizes it now live in one place, and a test asserts the writer's own output
satisfies the reader's predicate. Extending the human-readable tail is safe;
drifting the two apart is not.

If you change AUTOREG_DESCRIPTION, change nothing else -- is_autoregistered()
matches on the stable prefix by design.
"""

from __future__ import annotations

# The stable prefix. Recognition keys off THIS, never off the full string, so the
# parenthetical guidance can be reworded without breaking adoption.
AUTOREG_PREFIX = "auto-registered on SessionStart"

# What the SessionStart hook actually writes. The tail is guidance for a human
# reading the board; it carries no logic.
AUTOREG_DESCRIPTION = (
    f"{AUTOREG_PREFIX} (UNSCOPED — claims nothing; extend scope / take "
    "locks to claim files)"
)


def is_autoregistered(description: str | None) -> bool:
    """True if `description` is a SessionStart placeholder, not a real one.

    Prefix match, deliberately. An exact-equality check is what broke: the
    description grew a guidance tail and every reader silently stopped matching.
    A session that has been given a real description never starts with the
    marker, so the prefix is sufficient and stays correct as the tail evolves.
    """
    if not description:
        return False
    return description.startswith(AUTOREG_PREFIX)


def adopted_summary(adopting_session_id: str) -> str:
    """Summary written onto a placeholder row when a real session adopts it."""
    return f"adopted by {adopting_session_id[:8]} (start_session)"


def derived_working_description(scope: list[str], max_shown: int = 4) -> str:
    """Minimal honest description for a placeholder that has taken real locks.

    Not a substitute for a real one -- it says so -- but a session holding locks
    must not keep reading as an unclaimed orphan on the board.
    """
    shown = ", ".join(scope[:max_shown])
    ellipsis = "…" if len(scope) > max_shown else ""
    return (
        f"auto-registered; working scope: {shown}{ellipsis}"
        " (describe via start_session for a real summary)"
    )
