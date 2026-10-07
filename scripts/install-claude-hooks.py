#!/usr/bin/env python3
"""Install the deterministic ATS Claude hooks in precedence order."""

from __future__ import annotations

import argparse
import json
import os
import shlex
import tempfile
from pathlib import Path
from typing import Any

AUTOSTART_MODULE = "ai_team_sync.hooks.session_autostart"
CONTEXT_MODULE = "ai_team_sync.hooks.ats_context"
INBOX_MODULE = "ai_team_sync.hooks.override_inbox"


def _without_modules(groups: list[dict[str, Any]], modules: set[str]) -> list[dict[str, Any]]:
    cleaned: list[dict[str, Any]] = []
    for group in groups:
        copy = dict(group)
        copy["hooks"] = [
            hook
            for hook in group.get("hooks", [])
            if not any(f"-m {module}" in str(hook.get("command") or "") for module in modules)
        ]
        if copy["hooks"] or any(key != "hooks" for key in copy):
            cleaned.append(copy)
    return cleaned


def _prepend(
    settings: dict[str, Any], event: str, hooks: list[dict[str, Any]], modules: set[str]
) -> None:
    events = settings.setdefault("hooks", {})
    groups = _without_modules(list(events.get(event) or []), modules)
    # An unfiltered group runs for every occurrence of the event. Keep the ATS
    # gate in one such group so a matcher cannot silently omit a governed turn.
    target = next((group for group in groups if not group.get("matcher")), None)
    if target is None:
        target = {"hooks": []}
        groups.insert(0, target)
    else:
        groups.remove(target)
        groups.insert(0, target)
    target["hooks"] = hooks + list(target.get("hooks") or [])
    events[event] = groups


def install(settings_path: Path, python: str) -> None:
    settings_path = Path(settings_path)
    if settings_path.exists():
        settings = json.loads(settings_path.read_text())
    else:
        settings = {}
    if not isinstance(settings, dict):
        raise ValueError(f"{settings_path} must contain one JSON object")

    start_groups = list(settings.setdefault("hooks", {}).get("SessionStart") or [])
    startup_echo = ""
    for group in start_groups:
        for hook in group.get("hooks", []):
            command = str(hook.get("command") or "")
            if "echo-ambient.py" in command and "--mode session-start" in command:
                startup_echo = command
                break
        if startup_echo:
            break
    if startup_echo:
        for group in start_groups:
            group["hooks"] = [
                hook
                for hook in group.get("hooks", [])
                if str(hook.get("command") or "") != startup_echo
            ]
        settings["hooks"]["SessionStart"] = start_groups

    _prepend(
        settings,
        "SessionStart",
        [
            {
                "type": "command",
                "command": f"{python} -m {AUTOSTART_MODULE}",
                "timeout": 8,
                "statusMessage": "Registering and resolving session with ai-team-sync...",
            }
        ],
        {AUTOSTART_MODULE},
    )
    prompt_groups = list(settings.setdefault("hooks", {}).get("UserPromptSubmit") or [])
    supplement_command = ""
    for group in prompt_groups:
        for hook in group.get("hooks", []):
            command = str(hook.get("command") or "")
            if f"-m {CONTEXT_MODULE}" in command and "--supplement-command" in command:
                try:
                    parts = shlex.split(command)
                    supplement_command = parts[parts.index("--supplement-command") + 1]
                except (ValueError, IndexError):
                    pass
            elif "echo-ambient.py" in command and "--mode hook" in command:
                supplement_command = command
                break
        if supplement_command:
            break
    if supplement_command:
        for group in prompt_groups:
            group["hooks"] = [
                hook
                for hook in group.get("hooks", [])
                if str(hook.get("command") or "") != supplement_command
            ]
        settings["hooks"]["UserPromptSubmit"] = prompt_groups
    elif startup_echo:
        # A startup-only Echo installation must also move behind ATS. Echo's
        # prompt mode consumes the UserPromptSubmit payload and emits the same
        # supplemental packet at the correct point in the ordering contract.
        supplement_command = startup_echo.replace("--mode session-start", "--mode hook")

    context_command = f"{python} -m {CONTEXT_MODULE}"
    if supplement_command:
        context_command += (
            " --supplement-command " + shlex.quote(supplement_command) + " --supplement-timeout 90"
        )
    _prepend(
        settings,
        "UserPromptSubmit",
        [
            {
                "type": "command",
                "command": context_command,
                "timeout": 120,
            },
            {
                "type": "command",
                "command": f"{python} -m {INBOX_MODULE}",
                "timeout": 5,
            },
        ],
        {CONTEXT_MODULE, INBOX_MODULE},
    )

    rendered = json.dumps(settings, indent=2) + "\n"
    if settings_path.exists() and settings_path.read_text() == rendered:
        return
    settings_path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=f".{settings_path.name}.", dir=settings_path.parent)
    try:
        with os.fdopen(fd, "w") as handle:
            handle.write(rendered)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, settings_path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--settings", type=Path, default=Path.home() / ".claude" / "settings.json")
    parser.add_argument("--python", required=True)
    args = parser.parse_args()
    install(args.settings, args.python)
    print(f"ATS Claude hooks installed in {args.settings}")


if __name__ == "__main__":
    main()
