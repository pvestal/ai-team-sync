"""The deploy-time Claude hook wiring is ordered and idempotent."""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "install-claude-hooks.py"


def _module():
    spec = importlib.util.spec_from_file_location("install_claude_hooks", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    spec.loader.exec_module(module)
    return module


def test_installer_refuses_to_drop_legacy_repos_without_operator_config(tmp_path, monkeypatch):
    # Popping ATS_COORDINATED_REPOS with nowhere else declaring the governed
    # repos would silently turn the claim guard off on the next deploy.
    module = _module()
    monkeypatch.setenv("ATS_OPERATOR_CONFIG", str(tmp_path / "missing.toml"))
    settings = tmp_path / "settings.json"
    original = json.dumps({"env": {"ATS_COORDINATED_REPOS": "/private/repo-a"}})
    settings.write_text(original)

    with pytest.raises(ValueError, match="operator"):
        module.install(settings, "/opt/ats/bin/python")

    assert settings.read_text() == original


def test_installer_puts_ats_context_before_echo_and_is_idempotent(tmp_path, monkeypatch):
    module = _module()
    operator = tmp_path / "operator.toml"
    operator.write_text('[governance]\nrepositories = ["/private/repo-a", "/private/repo-b"]\n')
    monkeypatch.setenv("ATS_OPERATOR_CONFIG", str(operator))
    settings = tmp_path / "settings.json"
    settings.write_text(
        json.dumps(
            {
                "hooks": {
                    "SessionStart": [
                        {
                            "hooks": [
                                {
                                    "type": "command",
                                    "command": "python echo-ambient.py --mode session-start",
                                },
                            ]
                        }
                    ],
                    "UserPromptSubmit": [
                        {
                            "hooks": [
                                {
                                    "type": "command",
                                    "command": "python echo-ambient.py --mode hook",
                                    "async": True,
                                },
                            ]
                        }
                    ],
                },
                "env": {
                    "ATS_COORDINATED_REPOS": "/private/repo-a:/private/repo-b",
                    "ATS_AGENT": "claude-code",
                },
                "unrelated": {"preserved": True},
            }
        )
    )

    module.install(settings, "/opt/ats/bin/python")
    first = settings.read_text()
    module.install(settings, "/opt/ats/bin/python")
    second = settings.read_text()

    assert first == second
    body = json.loads(second)
    assert body["unrelated"] == {"preserved": True}
    assert body["env"] == {"ATS_AGENT": "claude-code"}
    prompt_commands = [
        hook["command"]
        for group in body["hooks"]["UserPromptSubmit"]
        for hook in group.get("hooks", [])
    ]
    assert prompt_commands[0].startswith("/opt/ats/bin/python -m ai_team_sync.hooks.ats_context ")
    assert "--supplement-command" in prompt_commands[0]
    assert "python echo-ambient.py --mode hook" in prompt_commands[0]
    assert prompt_commands[1] == ("/opt/ats/bin/python -m ai_team_sync.hooks.override_inbox")
    assert len(prompt_commands) == 2, "Echo must not remain as a parallel hook"
    context_hook = body["hooks"]["UserPromptSubmit"][0]["hooks"][0]
    assert context_hook["timeout"] == 120

    start_commands = [
        hook["command"]
        for group in body["hooks"]["SessionStart"]
        for hook in group.get("hooks", [])
    ]
    assert start_commands[0] == ("/opt/ats/bin/python -m ai_team_sync.hooks.session_autostart")
    assert all("echo-ambient.py" not in command for command in start_commands)


def test_installer_moves_startup_only_echo_behind_ats(tmp_path):
    module = _module()
    settings = tmp_path / "settings.json"
    settings.write_text(
        json.dumps(
            {
                "hooks": {
                    "SessionStart": [
                        {
                            "hooks": [
                                {
                                    "type": "command",
                                    "command": "python echo-ambient.py --mode session-start",
                                }
                            ]
                        }
                    ]
                }
            }
        )
    )

    module.install(settings, "/opt/ats/bin/python")

    body = json.loads(settings.read_text())
    start_commands = [
        hook["command"]
        for group in body["hooks"]["SessionStart"]
        for hook in group.get("hooks", [])
    ]
    prompt_commands = [
        hook["command"]
        for group in body["hooks"]["UserPromptSubmit"]
        for hook in group.get("hooks", [])
    ]
    assert all("echo-ambient.py" not in command for command in start_commands)
    assert "--supplement-command" in prompt_commands[0]
    assert "echo-ambient.py --mode hook" in prompt_commands[0]


def test_installer_adds_the_ats_caller_hook_for_ats_mcp_tools_idempotently(tmp_path):
    """#3677: the MCP server learns which agent (main thread or a Claude Code
    subagent) issues each ATS call from this PreToolUse hook. Existing
    PreToolUse groups (the edit and Bash guards, other matchers) are kept."""
    module = _module()
    settings = tmp_path / "settings.json"
    existing = [
        {"matcher": "Bash|mcp__.*", "hooks": [{"type": "command", "command": "gate.sh"}]},
        {"matcher": "Edit|Write", "hooks": [{
            "type": "command",
            "command": "/opt/ats/bin/python -m ai_team_sync.hooks.pre_tool_use_lockcheck"}]},
        # A stale copy under another matcher is moved, not duplicated.
        {"matcher": "mcp__.*", "hooks": [{
            "type": "command",
            "command": "/old/bin/python -m ai_team_sync.hooks.pre_tool_use_ats_caller"}]},
    ]
    settings.write_text(json.dumps({"hooks": {"PreToolUse": existing}}))

    module.install(settings, "/opt/ats/bin/python")
    first = settings.read_text()
    module.install(settings, "/opt/ats/bin/python")
    assert settings.read_text() == first

    groups = json.loads(first)["hooks"]["PreToolUse"]
    callers = [(g.get("matcher"), h) for g in groups for h in g.get("hooks", [])
               if "pre_tool_use_ats_caller" in h.get("command", "")]
    assert callers == [("mcp__ai-team-sync__.*", {
        "type": "command",
        "command": "/opt/ats/bin/python -m ai_team_sync.hooks.pre_tool_use_ats_caller",
        "timeout": 5})]
    assert "onFailure" not in callers[0][1]          # it must never block a call
    assert groups[0] == existing[0] and groups[1] == existing[1]
    assert all(g.get("matcher") != "mcp__.*" for g in groups)
