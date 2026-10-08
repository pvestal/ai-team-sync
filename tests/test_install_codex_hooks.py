"""Codex hooks.json installation is safe, reversible, and deterministic."""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "install-codex-hooks.py"


def _module():
    spec = importlib.util.spec_from_file_location("install_codex_hooks", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    spec.loader.exec_module(module)
    return module


def _commands(body, event):
    return [hook["command"] for group in body["hooks"][event] for hook in group.get("hooks", [])]


def test_install_is_idempotent_preserves_unrelated_hooks_and_shows_exact_diff(tmp_path):
    module = _module()
    hooks = tmp_path / "hooks.json"
    config = tmp_path / "config.toml"
    original = {
        "hooks": {
            "SessionStart": [{"hooks": [{"type": "command", "command": "notify-start"}]}],
            "AfterToolUse": [{"hooks": [{"type": "command", "command": "audit-tool"}]}],
        },
        "unrelated": {"preserved": True},
    }
    hooks.write_text(json.dumps(original))

    first_diff = module.apply(
        hooks,
        "/opt/ats/bin/python",
        config_path=config,
        module_root=Path("/candidate/src"),
    )
    first = hooks.read_text()
    second_diff = module.apply(
        hooks,
        "/opt/ats/bin/python",
        config_path=config,
        module_root=Path("/candidate/src"),
    )

    assert first_diff.startswith(f"--- {hooks}")
    assert '+      "SessionStart"' not in first_diff  # diff is exact JSON, not prose
    assert second_diff == ""
    assert hooks.read_text() == first
    body = json.loads(first)
    assert body["unrelated"] == {"preserved": True}
    assert _commands(body, "AfterToolUse") == ["audit-tool"]
    start = _commands(body, "SessionStart")
    prompt = _commands(body, "UserPromptSubmit")
    assert start[0] == (
        "env PYTHONPATH=/candidate/src /opt/ats/bin/python -m "
        "ai_team_sync.hooks.codex_session_autostart"
    )
    assert start[1] == "notify-start"
    assert prompt == [
        "env PYTHONPATH=/candidate/src /opt/ats/bin/python -m "
        "ai_team_sync.hooks.ats_context --agent codex"
    ]


def test_uninstall_removes_only_ats_hooks(tmp_path):
    module = _module()
    hooks = tmp_path / "hooks.json"
    config = tmp_path / "config.toml"
    hooks.write_text(
        module.render(
            {
                "hooks": {
                    "SessionStart": [{"hooks": [{"type": "command", "command": "notify-start"}]}]
                }
            },
            "/opt/ats/bin/python",
        )
    )

    diff = module.apply(hooks, "/opt/ats/bin/python", config_path=config, uninstall=True)

    assert "codex_session_autostart" in diff
    body = json.loads(hooks.read_text())
    assert _commands(body, "SessionStart") == ["notify-start"]
    assert "UserPromptSubmit" not in body["hooks"]


def test_malformed_existing_json_is_refused_without_overwrite(tmp_path):
    module = _module()
    hooks = tmp_path / "hooks.json"
    config = tmp_path / "config.toml"
    hooks.write_text('{"hooks": ')

    with pytest.raises(ValueError, match="refusing to overwrite malformed"):
        module.apply(hooks, "python", config_path=config)

    assert hooks.read_text() == '{"hooks": '


def test_inline_config_hooks_are_refused_without_overwrite(tmp_path):
    module = _module()
    hooks = tmp_path / "hooks.json"
    config = tmp_path / "config.toml"
    hooks.write_text('{"unrelated": true}\n')
    config.write_text("[hooks]\nSessionStart = []\n")

    with pytest.raises(ValueError, match="mixed Codex hook layers"):
        module.apply(hooks, "python", config_path=config)

    assert hooks.read_text() == '{"unrelated": true}\n'


def test_persisted_hook_trust_state_is_not_an_inline_hook_layer(tmp_path):
    module = _module()
    hooks = tmp_path / "hooks.json"
    config = tmp_path / "config.toml"
    config.write_text(
        '[hooks.state."hooks.json:session_start:0:0"]\n' 'trusted_hash = "sha256:abc"\n'
    )

    diff = module.apply(hooks, "python", config_path=config, dry_run=True)

    assert "codex_session_autostart" in diff


def test_dry_run_returns_diff_but_does_not_create_file(tmp_path):
    module = _module()
    hooks = tmp_path / "hooks.json"

    diff = module.apply(
        hooks,
        "python",
        config_path=tmp_path / "config.toml",
        dry_run=True,
    )

    assert diff.startswith("--- /dev/null")
    assert "codex_session_autostart" in diff
    assert not hooks.exists()
