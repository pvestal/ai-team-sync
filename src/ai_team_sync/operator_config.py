"""Machine-local operator policy shared by every ATS client.

The file deliberately lives outside Claude/Codex configuration so client hook
commands do not disclose workstation repository paths.  It is read on demand;
hook processes are short lived, so operator edits take effect next turn.
"""

from __future__ import annotations

import os
import tomllib
from pathlib import Path
from typing import Any


class OperatorConfigError(ValueError):
    """The shared operator configuration exists but is not trustworthy."""


def operator_config_path() -> Path:
    override = (os.environ.get("ATS_OPERATOR_CONFIG") or "").strip()
    return (
        Path(override) if override else Path.home() / ".config" / "ai-team-sync" / "operator.toml"
    )


def _normalize_repositories(values: Any, *, source: str) -> list[str]:
    if not isinstance(values, list) or any(not isinstance(value, str) for value in values):
        raise OperatorConfigError(f"{source}: governance.repositories must be an array of paths")
    roots: list[str] = []
    for value in values:
        value = value.strip()
        if not value or not os.path.isabs(value):
            raise OperatorConfigError(
                f"{source}: governed repository paths must be non-empty and absolute"
            )
        root = os.path.realpath(value).rstrip("/") or "/"
        if root not in roots:
            roots.append(root)
    return roots


#: Top-level tables operator.toml may carry. `restart_gate` (#3580) is read by
#: ai_team_sync.restart_gate; an ATS install older than #3580 rejects it, so the
#: table must be added only after every ATS install (pipx copy included) is upgraded.
_TOP_LEVEL_KEYS = {"governance", "restart_gate"}


def load_operator_document() -> dict:
    """The parsed operator.toml, {} when absent. Raises OperatorConfigError when
    the file exists but cannot be trusted."""
    path = operator_config_path()
    if not path.exists():
        return {}
    try:
        with path.open("rb") as handle:
            document = tomllib.load(handle)
    except (OSError, tomllib.TOMLDecodeError) as exc:
        raise OperatorConfigError(f"cannot read {path}: {exc}") from exc
    if not isinstance(document, dict):
        raise OperatorConfigError(f"{path}: root must be a TOML table")
    unknown = set(document) - _TOP_LEVEL_KEYS
    if unknown:
        raise OperatorConfigError(f"{path}: unknown top-level key(s) {sorted(unknown)}")
    return document


def governed_repositories() -> list[str]:
    """Return operator-governed roots, with the legacy env as explicit override."""
    legacy = (os.environ.get("ATS_COORDINATED_REPOS") or "").strip()
    if legacy:
        return _normalize_repositories(legacy.split(":"), source="ATS_COORDINATED_REPOS")

    path = operator_config_path()
    document = load_operator_document()
    if not document:
        return []
    governance = document.get("governance", {})
    if not isinstance(governance, dict):
        raise OperatorConfigError(f"{path}: [governance] must be a table")
    unknown = set(governance) - {"repositories"}
    if unknown:
        raise OperatorConfigError(f"{path}: unknown governance key(s) {sorted(unknown)}")
    return _normalize_repositories(governance.get("repositories", []), source=str(path))
