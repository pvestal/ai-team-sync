"""Tests for notification message formatting."""

from __future__ import annotations

import pytest

from ai_team_sync.notifications.dispatcher import format_message


def test_session_started_message():
    msg = format_message("session.started", {
        "developer": "Patrick",
        "agent": "claude-code",
        "scope": ["src/auth/**"],
        "description": "Refactoring auth middleware",
    })
    assert "Patrick" in msg
    assert "src/auth/**" in msg
    assert "claude-code" in msg
    assert "Refactoring auth middleware" in msg


def test_session_completed_message():
    msg = format_message("session.completed", {
        "developer": "Patrick",
        "branch": "feat/auth-jwt",
        "summary": "Migrated to JWT tokens",
    })
    assert "Patrick" in msg
    assert "feat/auth-jwt" in msg
    assert "Migrated to JWT tokens" in msg


def test_lock_conflict_message():
    msg = format_message("lock.conflict", {
        "developer": "Patrick",
        "paths": ["src/auth/middleware.py"],
        "pattern": "src/auth/**",
    })
    assert "CONFLICT" in msg
    assert "Patrick" in msg
    assert "src/auth/middleware.py" in msg


def test_decision_logged_message():
    msg = format_message("decision.logged", {
        "developer": "Patrick",
        "title": "Chose JWT over sessions",
        "chosen": "JWT",
        "rejected": "session cookies",
    })
    assert "Patrick" in msg
    assert "Chose JWT over sessions" in msg
    assert "JWT" in msg
    assert "session cookies" in msg


def test_unknown_event_message():
    msg = format_message("some.future.event", {"key": "value"})
    assert "some.future.event" in msg
