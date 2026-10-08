"""The deploy-time Claude hook wiring is ordered and idempotent."""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "install-claude-hooks.py"


def _module():
    spec = importlib.util.spec_from_file_location("install_claude_hooks", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    spec.loader.exec_module(module)
    return module


def test_installer_puts_ats_context_before_echo_and_is_idempotent(tmp_path):
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
