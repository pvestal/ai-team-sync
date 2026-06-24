#!/usr/bin/env python3
"""PostToolUse hook: auto-emit live presence after an agent edits a file.

Slice 2 of agent file-awareness. Wire it into Claude Code (or any agent that supports
a post-edit hook) so "who's editing what, right now" populates with ZERO manual effort:

    // ~/.claude/settings.json
    {
      "hooks": {
        "PostToolUse": [
          { "matcher": "Edit|Write|MultiEdit|NotebookEdit",
            "hooks": [ { "type": "command", "command": "ats-presence-hook" } ] }
        ]
      }
    }

The hook reads the PostToolUse JSON on stdin, extracts the edited file, and POSTs it to
the ats HTTP presence endpoint. It is fire-and-forget: presence has a TTL, so each edit
is a heartbeat ("editing now") and it ages out when edits stop. Identity + intent come
from env (set once per session): ATS_DEVELOPER, ATS_AGENT, ATS_INTENT. Any failure
(server down, bad payload) exits 0 — it must NEVER block or slow an agent's edit.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys

EDIT_TOOLS = {"Edit", "Write", "MultiEdit", "NotebookEdit"}

# Paths that are noise, not "what I'm working on" — skip so presence stays meaningful.
_SKIP_SUBSTR = ("/.git/", "/node_modules/", "/__pycache__/", "/.venv/", "/scratchpad/",
                "/.claude/", "/.playwright-mcp/")
_SKIP_PREFIX = ("/tmp/", "/var/tmp/", "/private/tmp/")


def _developer() -> str:
    if os.environ.get("ATS_DEVELOPER"):
        return os.environ["ATS_DEVELOPER"]
    try:
        name = subprocess.run(["git", "config", "user.name"], capture_output=True,
                              text=True, timeout=2).stdout.strip()
        if name:
            return name
    except Exception:
        pass
    return os.environ.get("USER", "unknown")


def _is_noise(path: str) -> bool:
    """True for temp/scratch/vendored paths that aren't meaningful work."""
    if any(path.startswith(p) for p in _SKIP_PREFIX):
        return True
    return any(s in path for s in _SKIP_SUBSTR)


def _git_root(path: str) -> str | None:
    """Walk up from the file's directory to the enclosing git repo root, if any."""
    d = os.path.dirname(os.path.abspath(path))
    while True:
        if os.path.isdir(os.path.join(d, ".git")):
            return d
        parent = os.path.dirname(d)
        if parent == d:
            return None
        d = parent


def _display_path(path: str, cwd: str | None) -> str:
    """Clean, legible path: repo-relative if in a git repo, else cwd-relative if that
    stays inside cwd, else the bare basename (never an ugly ../../ escape)."""
    root = _git_root(path)
    if root:
        try:
            return os.path.relpath(path, root)
        except Exception:
            pass
    if cwd and os.path.isabs(path):
        rel = os.path.relpath(path, cwd)
        if not rel.startswith(".."):
            return rel
    return os.path.basename(path)


def build_presence(payload: dict, env: dict) -> dict | None:
    """Pure: PostToolUse payload + env -> presence POST body, or None to skip.

    Returns None for non-edit tools, payloads with no file_path, or noise paths
    (temp/scratch/vendored) — so presence reflects real work, not churn.
    """
    if payload.get("tool_name") not in EDIT_TOOLS:
        return None
    file_path = (payload.get("tool_input") or {}).get("file_path")
    if not file_path or _is_noise(file_path):
        return None
    rel = _display_path(file_path, payload.get("cwd") or env.get("PWD"))
    return {
        "developer": env.get("ATS_DEVELOPER") or _developer(),
        "agent": env.get("ATS_AGENT", "claude-code"),
        "files": [rel],
        "intent": env.get("ATS_INTENT", ""),
    }


def main() -> None:
    try:
        payload = json.load(sys.stdin)
    except Exception:
        sys.exit(0)  # no/!json payload — never block the edit

    body = build_presence(payload, dict(os.environ))
    if body is None:
        sys.exit(0)

    server = os.environ.get("ATS_SERVER_URL", "http://localhost:8400")
    try:
        import httpx
        with httpx.Client(timeout=2) as client:
            client.post(f"{server}/api/presence", json=body)
    except Exception:
        pass  # server down / network — fire-and-forget, never block
    sys.exit(0)


if __name__ == "__main__":
    main()
