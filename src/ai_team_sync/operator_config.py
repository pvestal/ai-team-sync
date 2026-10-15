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
#: `context` and `admission` (#3591) carry the same caveat: add them to
#: operator.toml only after every install has this code.
_TOP_LEVEL_KEYS = {"governance", "restart_gate", "context", "admission"}

ADMISSION_MODES = ("off", "shadow", "enforce")


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


def _table(name: str, allowed: set[str]) -> dict:
    path = operator_config_path()
    table = load_operator_document().get(name, {})
    if not isinstance(table, dict):
        raise OperatorConfigError(f"{path}: [{name}] must be a table")
    unknown = set(table) - allowed
    if unknown:
        raise OperatorConfigError(f"{path}: unknown {name} key(s) {sorted(unknown)}")
    return table


def context_repositories() -> list[str]:
    """Repositories that get ATS-first CONTEXT but are not governed.

    Context is a brief: tickets, unfinished branches, prior work. Governance
    is enforcement: lock and commit guards. Listing a repository here never
    turns enforcement on for it (#3591).
    """
    table = _table("context", {"repositories", "aliases"})
    return _normalize_repositories(table.get("repositories", []),
                                   source=f"{operator_config_path()} [context]")


def context_aliases() -> dict[str, list[str]]:
    """Operator-listed short names per repository, e.g. {"/path": ["ats"]}."""
    raw = _table("context", {"repositories", "aliases"}).get("aliases", {})
    if not isinstance(raw, dict) or any(
            not isinstance(v, list) or any(not isinstance(a, str) for a in v)
            for v in raw.values()):
        raise OperatorConfigError(
            f"{operator_config_path()}: context.aliases must map paths to arrays of names")
    return {_normalize_repositories([path], source="context.aliases")[0]: list(names)
            for path, names in raw.items()}


def admission_policy() -> tuple[str, list[str]]:
    """(mode, enforce_repositories). Absent config is shadow: admission is
    computed and recorded, and refuses nothing (#3591)."""
    table = _table("admission", {"mode", "enforce_repositories"})
    mode = table.get("mode", "shadow")
    if mode not in ADMISSION_MODES:
        raise OperatorConfigError(
            f"{operator_config_path()}: admission.mode must be one of {ADMISSION_MODES}")
    repos = _normalize_repositories(table.get("enforce_repositories", []),
                                    source=f"{operator_config_path()} [admission]")
    return mode, repos


def admission_mode_for(repo_root: str) -> str:
    """Enforcement applies only to repositories the operator listed; every
    other repository stays in shadow even when the global mode is enforce."""
    mode, repos = admission_policy()
    if mode != "enforce":
        return mode
    root = os.path.realpath(repo_root).rstrip("/") if repo_root else ""
    return "enforce" if root and root in repos else "shadow"
