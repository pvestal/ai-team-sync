#!/usr/bin/env python3
"""Install or remove Codex ATS-first lifecycle hooks without clobbering peers."""

from __future__ import annotations

import argparse
import difflib
import json
import os
import shlex
import tempfile
import tomllib
from pathlib import Path
from typing import Any

AUTOSTART_MODULE = "ai_team_sync.hooks.codex_session_autostart"
CONTEXT_MODULE = "ai_team_sync.hooks.ats_context"
OWN_MODULES = {AUTOSTART_MODULE, CONTEXT_MODULE}


def _read_json(path: Path) -> tuple[dict[str, Any], str]:
    if not path.exists():
        return {}, ""
    original = path.read_text()
    try:
        body = json.loads(original)
    except json.JSONDecodeError as exc:
        raise ValueError(f"refusing to overwrite malformed {path}: {exc}") from exc
    if not isinstance(body, dict):
        raise ValueError(f"refusing to overwrite {path}: root must be one JSON object")
    hooks = body.get("hooks")
    if hooks is not None and not isinstance(hooks, dict):
        raise ValueError(f"refusing to overwrite {path}: hooks must be one JSON object")
    return body, original


def _refuse_inline_hooks(config_path: Path) -> None:
    if not config_path.exists():
        return
    try:
        config = tomllib.loads(config_path.read_text())
    except tomllib.TOMLDecodeError as exc:
        raise ValueError(f"cannot inspect malformed {config_path}: {exc}") from exc
    if "hooks" in config:
        raise ValueError(
            f"refusing mixed Codex hook layers: remove [hooks] from {config_path} "
            "or uninstall hooks.json first"
        )


def _is_ours(hook: Any) -> bool:
    if not isinstance(hook, dict):
        return False
    command = str(hook.get("command") or "")
    return any(f"-m {module}" in command for module in OWN_MODULES)


def _without_ours(groups: Any) -> list[Any]:
    if not isinstance(groups, list):
        raise ValueError("hook event entries must be arrays")
    cleaned: list[Any] = []
    for group in groups:
        if not isinstance(group, dict):
            raise ValueError("hook groups must be JSON objects")
        hooks = group.get("hooks", [])
        if not isinstance(hooks, list):
            raise ValueError("hook group hooks must be arrays")
        if any(not isinstance(hook, dict) for hook in hooks):
            raise ValueError("hook entries must be JSON objects")
        copy = dict(group)
        copy["hooks"] = [hook for hook in hooks if not _is_ours(hook)]
        if copy["hooks"] or any(key != "hooks" for key in copy):
            cleaned.append(copy)
    return cleaned


def _install_event(config: dict[str, Any], event: str, hook: dict[str, Any]) -> None:
    events = config.setdefault("hooks", {})
    groups = _without_ours(events.get(event, []))
    target = next((group for group in groups if not group.get("matcher")), None)
    if target is None:
        target = {"hooks": []}
        groups.insert(0, target)
    else:
        groups.remove(target)
        groups.insert(0, target)
    target["hooks"] = [hook, *target.get("hooks", [])]
    events[event] = groups


def _command(python: str, module: str, module_root: Path | None, governed_repos: str) -> str:
    parts: list[str] = []
    environment: list[str] = []
    if module_root is not None:
        environment.append(f"PYTHONPATH={module_root}")
    if governed_repos:
        environment.append(f"ATS_COORDINATED_REPOS={governed_repos}")
    if environment:
        parts.extend(["env", *environment])
    parts.extend([python, "-m", module])
    return shlex.join(parts)


def render(
    existing: dict[str, Any],
    python: str,
    *,
    module_root: Path | None = None,
    governed_repos: str = "",
    supplement_command: str = "",
    uninstall: bool = False,
) -> str:
    # JSON round-trip is an intentional deep copy that also rejects objects not
    # representable in the destination format.
    config = json.loads(json.dumps(existing))
    events = config.setdefault("hooks", {})
    for event in ("SessionStart", "UserPromptSubmit"):
        if event in events:
            cleaned = _without_ours(events[event])
            if cleaned:
                events[event] = cleaned
            else:
                events.pop(event)
    if uninstall:
        if not events:
            config.pop("hooks", None)
        return json.dumps(config, indent=2) + "\n"

    _install_event(
        config,
        "SessionStart",
        {
            "type": "command",
            "command": _command(python, AUTOSTART_MODULE, module_root, governed_repos),
            "timeout": 8,
            "statusMessage": "Registering Codex lifecycle with ai-team-sync...",
        },
    )
    context = _command(python, CONTEXT_MODULE, module_root, governed_repos) + " --agent codex"
    if supplement_command:
        context += " --supplement-command " + shlex.quote(supplement_command)
        context += " --supplement-timeout 90"
    _install_event(
        config,
        "UserPromptSubmit",
        {"type": "command", "command": context, "timeout": 120},
    )
    return json.dumps(config, indent=2) + "\n"


def _diff(path: Path, original: str, rendered: str) -> str:
    before = original.splitlines(keepends=True)
    after = rendered.splitlines(keepends=True)
    return "".join(
        difflib.unified_diff(
            before,
            after,
            fromfile=str(path) if original else "/dev/null",
            tofile=str(path),
        )
    )


def apply(
    hooks_path: Path,
    python: str,
    *,
    config_path: Path,
    module_root: Path | None = None,
    governed_repos: str = "",
    supplement_command: str = "",
    uninstall: bool = False,
    dry_run: bool = False,
) -> str:
    hooks_path = Path(hooks_path)
    existing, original = _read_json(hooks_path)
    if not uninstall:
        _refuse_inline_hooks(Path(config_path))
    rendered = render(
        existing,
        python,
        module_root=module_root,
        governed_repos=governed_repos,
        supplement_command=supplement_command,
        uninstall=uninstall,
    )
    diff = _diff(hooks_path, original, rendered)
    if not diff or dry_run:
        return diff

    hooks_path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=f".{hooks_path.name}.", dir=hooks_path.parent)
    try:
        with os.fdopen(fd, "w") as handle:
            handle.write(rendered)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, hooks_path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)
    return diff


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--hooks", type=Path, default=Path.home() / ".codex" / "hooks.json")
    parser.add_argument("--config", type=Path, default=Path.home() / ".codex" / "config.toml")
    parser.add_argument("--python", required=True)
    parser.add_argument("--module-root", type=Path)
    parser.add_argument("--governed-repos", default=os.environ.get("ATS_COORDINATED_REPOS", ""))
    parser.add_argument("--supplement-command", default="")
    parser.add_argument("--uninstall", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    try:
        diff = apply(
            args.hooks,
            args.python,
            config_path=args.config,
            module_root=args.module_root,
            governed_repos=args.governed_repos,
            supplement_command=args.supplement_command,
            uninstall=args.uninstall,
            dry_run=args.dry_run,
        )
    except ValueError as exc:
        parser.error(str(exc))
    print(diff or "(no changes)", end="" if diff else "\n")
    action = "would update" if args.dry_run else "updated"
    print(f"Codex ATS hooks {action}: {args.hooks}")


if __name__ == "__main__":
    main()
