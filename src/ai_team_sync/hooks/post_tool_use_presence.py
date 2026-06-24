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


def _relativize(path: str, cwd: str | None) -> str:
    if not path:
        return path
    try:
        if cwd and os.path.isabs(path):
            return os.path.relpath(path, cwd)
    except Exception:
        pass
    return path


def build_presence(payload: dict, env: dict) -> dict | None:
    """Pure: PostToolUse payload + env -> presence POST body, or None to skip.

    Returns None for non-edit tools or payloads with no file_path (nothing to report).
    """
    if payload.get("tool_name") not in EDIT_TOOLS:
        return None
    file_path = (payload.get("tool_input") or {}).get("file_path")
    if not file_path:
        return None
    rel = _relativize(file_path, payload.get("cwd") or env.get("PWD"))
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
