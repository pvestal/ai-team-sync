"""Tests for agent identity resolution (_detect_agent).

Runnable two ways:
    pytest tests/test_detect_agent.py
    python tests/test_detect_agent.py
"""

import os
import sys

# src/ layout: make the package importable when run standalone.
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from ai_team_sync.cli import _detect_agent

_RELEVANT = ("ATS_AGENT", "CLAUDE_CODE", "CURSOR_SESSION", "COPILOT_WORKSPACE")


def _run(env_overrides):
    saved = dict(os.environ)
    try:
        for k in list(os.environ):
            if k in _RELEVANT or k.startswith("CODEX"):
                os.environ.pop(k, None)
        os.environ.update(env_overrides)
        return _detect_agent()
    finally:
        os.environ.clear()
        os.environ.update(saved)


def test_explicit_override_wins():
    assert _run({"ATS_AGENT": "codex", "CLAUDE_CODE": "1"}) == "codex"
    assert _run({"ATS_AGENT": "ollama:qwen2.5-coder"}) == "ollama:qwen2.5-coder"
    assert _run({"ATS_AGENT": "  codex  "}) == "codex"


def test_blank_override_falls_through():
    assert _run({"ATS_AGENT": "", "CLAUDE_CODE": "1"}) == "claude-code"
    assert _run({"ATS_AGENT": "   ", "CLAUDE_CODE": "1"}) == "claude-code"


def test_codex_autodetect():
    assert _run({"CODEX_SANDBOX": "seatbelt"}) == "codex"
    assert _run({"CODEX_SANDBOX_NETWORK_DISABLED": "1"}) == "codex"


def test_known_signatures():
    assert _run({"CLAUDE_CODE": "1"}) == "claude-code"
    assert _run({"CURSOR_SESSION": "x"}) == "cursor"
    assert _run({"COPILOT_WORKSPACE": "x"}) == "copilot-workspace"


def test_unknown_default():
    assert _run({}) == "unknown"


def test_precedence_claude_over_codex():
    assert _run({"CLAUDE_CODE": "1", "CODEX_SANDBOX": "seatbelt"}) == "claude-code"


if __name__ == "__main__":
    passed = 0
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print(f"  ok  {name}")
            passed += 1
    print(f"\n{passed} passed")
