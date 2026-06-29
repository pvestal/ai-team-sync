"""Unit tests for the override-inbox hook formatter (UserPromptSubmit surfacer)."""
from __future__ import annotations

from ai_team_sync.hooks.override_inbox import format_inbox

ME = "owner-session-1"


def test_none_when_no_requests():
    assert format_inbox([], ME) is None


def test_shows_only_incoming_pending_for_me():
    reqs = [
        # incoming + pending -> shown
        {"id": "aaaaaaaa-1", "owner_session_id": ME, "status": "pending",
         "requester_developer": "sarah", "conflicting_pattern": "src/auth/**",
         "justification": "hotfix for login"},
        # I'm the requester, not the owner -> hidden
        {"id": "bbbbbbbb-2", "owner_session_id": "other", "requester_session_id": ME,
         "status": "pending", "conflicting_pattern": "src/x/**"},
        # incoming but already resolved -> hidden
        {"id": "cccccccc-3", "owner_session_id": ME, "status": "approved",
         "conflicting_pattern": "src/y/**"},
    ]
    note = format_inbox(reqs, ME)
    assert note is not None
    assert "1 override request(s) awaiting YOUR response" in note
    assert "aaaaaaaa" in note and "sarah" in note and "src/auth/**" in note
    assert "hotfix for login" in note
    # the non-incoming / resolved ones must not leak in
    assert "bbbbbbbb" not in note and "cccccccc" not in note


def test_none_when_only_outgoing_or_resolved():
    reqs = [
        {"id": "d", "owner_session_id": "other", "requester_session_id": ME, "status": "pending"},
        {"id": "e", "owner_session_id": ME, "status": "denied"},
    ]
    assert format_inbox(reqs, ME) is None
