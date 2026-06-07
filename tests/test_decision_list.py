"""Tests for `ats decision list` scope behavior.

`decision list` used to always filter to the active session, so an agent could
never read the team-wide decision log mid-session. `--all` forces the full log.

Dependency-free: patches the HTTP layer by hand (no pytest fixtures needed).
"""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from click.testing import CliRunner

import ai_team_sync.cli as m


class _FakeResp:
    def json(self):
        return []


def _invoke(argv, active_session):
    captured = {}

    def fake_api(method, path, **kwargs):
        captured["params"] = kwargs.get("params")
        return _FakeResp()

    orig_api, orig_load = m._api, m._load_active_session
    m._api = fake_api
    m._load_active_session = lambda: active_session
    try:
        result = CliRunner().invoke(m.cli, argv)
    finally:
        m._api, m._load_active_session = orig_api, orig_load
    return result, captured


def test_all_flag_ignores_active_session():
    result, captured = _invoke(["decision", "list", "--all"], active_session="sess-123")
    assert result.exit_code == 0, result.output
    assert captured["params"] == {}


def test_default_filters_to_active_session():
    result, captured = _invoke(["decision", "list"], active_session="sess-123")
    assert result.exit_code == 0, result.output
    assert captured["params"] == {"session_id": "sess-123"}


def test_default_with_no_session_lists_all():
    result, captured = _invoke(["decision", "list"], active_session=None)
    assert result.exit_code == 0, result.output
    assert captured["params"] == {}


if __name__ == "__main__":
    passed = 0
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print(f"  ok  {name}")
            passed += 1
    print(f"\n{passed} passed")
