"""Presence carries a one-line `intent` (slice #1 of agent file-awareness): what an
agent/dev is doing, not just which files are open — surfaced in get_all + dashboard.
"""
from __future__ import annotations

import pytest

from ai_team_sync.presence import PresenceStore, store
from ai_team_sync.routers.dashboard import _render


def test_intent_roundtrips_through_store():
    s = PresenceStore()
    s.update("alice", "claude-code", ["src/auth/jwt.py"], intent="rewriting token validation")
    rows = s.get_all()
    assert rows[0]["intent"] == "rewriting token validation"


def test_intent_defaults_empty_and_is_backward_compatible():
    # old callers (no intent arg) still work — intent just defaults to "".
    s = PresenceStore()
    s.update("bob", "cursor", ["frontend/Nav.tsx"])
    assert s.get_all()[0]["intent"] == ""


def test_dashboard_renders_intent_line():
    html = _render([{
        "developer": "alice", "agent": "claude-code",
        "files": ["src/auth/jwt.py"], "intent": "rewriting token validation",
    }])
    assert "rewriting token validation" in html
    assert 'class="intent"' in html


def test_dashboard_omits_intent_line_when_absent():
    html = _render([{
        "developer": "bob", "agent": "cursor", "files": ["frontend/Nav.tsx"], "intent": "",
    }])
    assert 'class="intent"' not in html


def test_dashboard_escapes_intent():
    html = _render([{
        "developer": "x", "agent": "y", "files": ["a.py"], "intent": "<script>alert(1)</script>",
    }])
    assert "<script>alert(1)</script>" not in html
    assert "&lt;script&gt;" in html


@pytest.fixture(autouse=True)
def _clear_global_store():
    # keep the module-level singleton clean for any test that touches it
    store._devs.clear()
    yield
    store._devs.clear()
