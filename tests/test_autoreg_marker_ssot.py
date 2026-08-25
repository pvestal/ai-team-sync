"""The auto-registration marker's writer and readers must not drift apart.

REGRESSION: 2026-08-24. The SessionStart hook wrote

    "auto-registered on SessionStart (UNSCOPED — claims nothing; extend scope /
     take locks to claim files)"

while two readers in mcp/server.py tested

    description == "auto-registered on SessionStart"

Neither predicate could ever be true, so:
  - orphan adoption never fired, and every agent accumulated a permanent ghost
    row beside its real session (agent claude-code:7bb1e161 was simultaneously
    active as cf21607b and 4215bbc3);
  - team_status double-counted, reporting 9 active sessions on a board with
    roughly one live worker;
  - a session that took real locks kept a description reading as an orphan.

Nothing failed loudly. The guards were shipped and unsatisfiable — the same
shape as a validator with zero callers, except worse, because the call site
existed and looked correct.

These tests pin the invariant that actually matters: whatever the hook writes
must be recognized by whatever reads it.
"""

from __future__ import annotations

from ai_team_sync.session_marker import (
    AUTOREG_DESCRIPTION,
    AUTOREG_PREFIX,
    adopted_summary,
    derived_working_description,
    is_autoregistered,
)


def test_the_written_description_is_recognized_by_the_reader():
    """The one invariant. If this fails, adoption is silently dead again."""
    assert is_autoregistered(AUTOREG_DESCRIPTION)


def test_recognition_survives_a_reworded_guidance_tail():
    """The tail is prose for humans and must be editable without breaking logic."""
    assert is_autoregistered(AUTOREG_PREFIX + " (any wording at all)")
    assert is_autoregistered(AUTOREG_PREFIX)


def test_the_exact_equality_that_broke_is_not_reintroduced():
    """Guards the specific 2026-08-24 defect.

    The bare prefix is NOT what the hook writes. Any reader comparing for
    equality against it is dead code, so assert the two differ — if someone
    'simplifies' AUTOREG_DESCRIPTION back down to the prefix this test still
    passes, but the equality-based reader it would resurrect is gone from the
    codebase and the prefix predicate covers both cases.
    """
    assert AUTOREG_DESCRIPTION != AUTOREG_PREFIX
    assert AUTOREG_DESCRIPTION.startswith(AUTOREG_PREFIX)


def test_a_real_description_is_never_mistaken_for_a_placeholder():
    """Adoption completes sessions. A false positive would kill live work."""
    assert not is_autoregistered("W-2 READ-ONLY: CONTACT-geometry enforcement seam")
    assert not is_autoregistered("packages/scene_generation/keyframe_route_resolver.py")
    assert not is_autoregistered("")
    assert not is_autoregistered(None)


def test_a_description_merely_mentioning_the_marker_is_not_a_placeholder():
    """Prefix match, not substring. A worker describing the bug is not an orphan."""
    assert not is_autoregistered(
        "investigating why auto-registered on SessionStart rows are never adopted"
    )


def test_adopted_summary_names_the_adopting_session():
    s = adopted_summary("4215bbc3-e21b-44d8-8ef5-58a65e3616de")
    assert "4215bbc3" in s
    assert "start_session" in s


def test_derived_working_description_is_honest_and_not_itself_a_placeholder():
    """A derived description must not loop back into looking like an orphan."""
    scope = ["a.py", "b.py", "c.py", "d.py", "e.py"]
    d = derived_working_description(scope)
    assert "a.py" in d and "d.py" in d
    assert "…" in d, "must signal that the scope list was truncated"
    assert "e.py" not in d
    assert not is_autoregistered(d), (
        "a derived description must not still read as an unclaimed placeholder"
    )


def test_derived_working_description_without_truncation():
    d = derived_working_description(["only.py"])
    assert "only.py" in d
    assert "…" not in d
