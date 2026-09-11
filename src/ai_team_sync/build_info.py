"""Machine-readable build identity for the ATS server and MCP.

Why this exists. A stdio MCP server is spawned once per client session and
holds its tool catalog for that session's whole life. `pipx install --force`
replaces files on disk; it cannot reach into a process that is already running.
So the REST service (restarted on deploy) and a client's MCP process (not) can
be running different revisions, and on 2026-09-11 they were: a Codex session
started 13:45:39 kept a catalog with no delegation tools while REST had served
/api/delegations since 14:01. Nothing in either surface could say which build it
was, so the mismatch read as "the feature was never implemented".

A client can now ask both surfaces what they are and compare, instead of
inferring from process paths or file timestamps.
"""

from __future__ import annotations

import os
import subprocess
import time
from pathlib import Path
from typing import Any

_PKG = Path(__file__).resolve().parent
# Written into the package at deploy time (scripts/deploy.sh), so the COPY that
# pipx installs carries the commit it was built from. Gitignored: it is build
# output, not source. A .py module rather than a data file because setuptools
# packages modules automatically — a JSON file needs package-data config and
# silently did not ship, which is how the first deploy reported commit
# "unknown" while claiming success.

_PROCESS_STARTED = time.time()


def _from_stamp() -> dict[str, Any] | None:
    try:
        from ai_team_sync import _build_stamp as stamp  # type: ignore[attr-defined]
    except Exception:  # noqa: BLE001 — absent in a plain checkout
        return None
    commit = getattr(stamp, "COMMIT", "")
    if not commit:
        return None
    return {"commit": commit, "dirty": getattr(stamp, "DIRTY", None),
            "built_at": getattr(stamp, "BUILT_AT", None),
            "revision_source": "stamp"}


def _from_git() -> dict[str, Any] | None:
    """Only meaningful when running straight from a checkout (tests, dev)."""
    try:
        commit = subprocess.run(["git", "rev-parse", "--short", "HEAD"],
                                cwd=_PKG, capture_output=True, text=True,
                                check=True, timeout=3).stdout.strip()
        dirty = bool(subprocess.run(["git", "status", "--porcelain"], cwd=_PKG,
                                    capture_output=True, text=True,
                                    timeout=3).stdout.strip())
        return {"commit": commit, "dirty": dirty, "revision_source": "git"}
    except Exception:  # noqa: BLE001
        return None


def _version() -> str:
    try:
        from importlib.metadata import version
        return version("ai-team-sync")
    except Exception:  # noqa: BLE001
        return "unknown"


def identity(component: str) -> dict[str, Any]:
    """What this running process actually is. Never raises."""
    rev = _from_stamp() or _from_git() or {"commit": "unknown",
                                           "revision_source": "unavailable"}
    return {
        "component": component,
        "commit": rev.get("commit", "unknown"),
        "revision_source": rev.get("revision_source"),
        "dirty": rev.get("dirty"),
        "built_at": rev.get("built_at"),
        "version": _version(),
        "package_path": str(_PKG),
        "pid": os.getpid(),
        "process_started_at": time.strftime("%Y-%m-%dT%H:%M:%S",
                                            time.localtime(_PROCESS_STARTED)),
    }


def summary_line(ident: dict[str, Any]) -> str:
    dirty = " (dirty)" if ident.get("dirty") else ""
    return (f"{ident['component']} commit {ident['commit']}{dirty} "
            f"v{ident['version']} pid {ident['pid']} "
            f"up since {ident['process_started_at']}")
