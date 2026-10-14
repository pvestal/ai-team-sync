"""commit_guard.resolve_identity / identity_problem (#3581 review finding 1),
without a server. End-to-end refusals are in test_commit_guard_live.py."""

from __future__ import annotations

import pytest

from ai_team_sync.hooks import commit_guard as cg

CID = "bbbbbbbb-0000-0000-0000-000000000002"


@pytest.fixture
def state(tmp_path, monkeypatch):
    for var in ("ATS_SESSION_ID", "CLAUDE_CODE_SESSION_ID", "ATS_SESSION", "CLAUDE_PID",
                "ATS_DELEGATION"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("ATS_STATE_DIR", str(tmp_path))
    return tmp_path


def test_pointer_wins_and_contradicting_env_is_a_conflict(state, monkeypatch):
    (state / f".ats_session_{CID[:8]}").write_text("session-b")
    monkeypatch.setenv("ATS_SESSION_ID", "session-a")
    ident = cg.resolve_identity(CID)
    assert ident.session_id == "session-b" and ident.source == "per_session"
    assert "session-a" in ident.conflict and "session-b" in ident.conflict


def test_cid_from_environment_is_used_when_no_payload(state, monkeypatch):
    (state / f".ats_session_{CID[:8]}").write_text("session-b")
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", CID)
    monkeypatch.setenv("ATS_SESSION_ID", "session-a")
    assert cg.resolve_identity().conflict


def test_matching_env_is_not_a_conflict(state, monkeypatch):
    (state / f".ats_session_{CID[:8]}").write_text("session-b")
    monkeypatch.setenv("ATS_SESSION_ID", "session-b")
    ident = cg.resolve_identity(CID)
    assert ident.session_id == "session-b" and not ident.conflict


def test_env_only_identity_is_self_asserted(state, monkeypatch):
    monkeypatch.setenv("ATS_SESSION_ID", "session-a")
    ident = cg.resolve_identity(CID)  # cid known, but no pointer for it
    assert (ident.session_id, ident.source, ident.self_asserted) == ("session-a", "env", True)


def test_shared_global_pointer_is_never_an_identity(state):
    (state / ".ats_session").write_text("whoever-wrote-last")
    assert cg.resolve_identity(CID).session_id is None


def test_server_refusing_ownership_makes_identity_invalid_without_further_calls():
    """caller_identity_unresolved is the server's #2741 verdict (kernel uid of the
    socket vs creator_uid): a session created by another OS account lands here."""
    ident = cg.Identity("session-of-another-account", "env")
    problem = cg.identity_problem(ident, [{"path": "f.txt", "caller_identity_unresolved": True}])
    assert "owned by this OS account" in problem


def test_no_identity_has_no_problem():
    assert cg.identity_problem(cg.Identity(None, "none"), []) == ""
