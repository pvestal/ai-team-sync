"""MCP-server agent identity (#1556): the server had its own stale detect_agent
that only checked CLAUDE_CODE (underscore) and ignored ATS_AGENT, so concurrent
Claude sessions all showed 'developer (unknown)' and were indistinguishable.
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from ai_team_sync.mcp.server import detect_agent, session_agent_label

_CLEAR = ("ATS_AGENT", "CLAUDE_CODE", "CLAUDECODE", "CURSOR_SESSION",
          "COPILOT_WORKSPACE", "CLAUDE_CODE_SESSION_ID", "ATS_SESSION")


def _run(fn, env):
    saved = dict(os.environ)
    try:
        for k in _CLEAR:
            os.environ.pop(k, None)
        for k in list(os.environ):
            if k.startswith("CODEX"):
                os.environ.pop(k, None)
        os.environ.update(env)
        return fn()
    finally:
        os.environ.clear()
        os.environ.update(saved)


def test_claudecode_no_underscore_detected():
    # the #1556 root cause: Claude Code sets CLAUDECODE, not CLAUDE_CODE
    assert _run(detect_agent, {"CLAUDECODE": "1"}) == "claude-code"


def test_ats_agent_explicit_wins():
    assert _run(detect_agent, {"ATS_AGENT": "claude-code", "CLAUDECODE": "1"}) == "claude-code"
    assert _run(detect_agent, {"ATS_AGENT": "ollama:qwen"}) == "ollama:qwen"


def test_unknown_when_no_signal():
    assert _run(detect_agent, {}) == "unknown"


def test_session_label_distinct_per_session():
    a = _run(session_agent_label, {"CLAUDECODE": "1", "CLAUDE_CODE_SESSION_ID": "aaaaaaaa-1111"})
    b = _run(session_agent_label, {"CLAUDECODE": "1", "CLAUDE_CODE_SESSION_ID": "bbbbbbbb-2222"})
    assert a == "claude-code:aaaaaaaa"
    assert b == "claude-code:bbbbbbbb"
    assert a != b, "concurrent sessions must be distinguishable"


def test_session_label_no_token_falls_back_to_base():
    assert _run(session_agent_label, {"CLAUDECODE": "1"}) == "claude-code"
    assert _run(session_agent_label, {}) == "unknown"


if __name__ == "__main__":
    n = 0
    for k, v in sorted(globals().items()):
        if k.startswith("test_") and callable(v):
            v(); print(f"  ok  {k}"); n += 1
    print(f"\n{n} passed")
