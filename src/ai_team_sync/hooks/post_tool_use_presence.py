#!/usr/bin/env python3
"""PostToolUse hook: record reported file actions and emit live edit presence.

Slice 2 of agent file-awareness. Wire it into Claude Code (or any agent that supports
a post-edit hook) so "who's editing what, right now" populates with ZERO manual effort:

    // ~/.claude/settings.json
    {
      "hooks": {
        "PostToolUse": [
          { "matcher": "Read|Edit|Write|MultiEdit|NotebookEdit",
            "hooks": [ { "type": "command", "command": "ats-presence-hook" } ] }
        ]
      }
    }

The hook reads the PostToolUse JSON on stdin and records instrumented reads and edits.
Edits also POST to the presence endpoint. Presence has a TTL, so it ages out when
edits stop. Identity + intent come
from env (set once per session): ATS_DEVELOPER, ATS_AGENT, ATS_INTENT. Any failure
(server down, bad payload) exits 0 — it must NEVER block or slow an agent's edit.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys

from ai_team_sync.git_utils import resolve_repo_roots as _roots

EDIT_TOOLS = {"Edit", "Write", "MultiEdit", "NotebookEdit"}
READ_TOOLS = {"Read"}

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


def _display_path(path: str, cwd: str | None) -> str:
    """Clean, legible path: repo-relative if in a git repo, else cwd-relative if that
    stays inside cwd, else the bare basename (never an ugly ../../ escape)."""
    root = _roots(path)[0]
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
    body = {
        "developer": env.get("ATS_DEVELOPER") or _developer(),
        "agent": _agent_label(payload, env),
        "files": [rel],
        "intent": env.get("ATS_INTENT", ""),
    }
    sid = _session_id(payload, env)
    if sid:
        body["session_id"] = sid
    return body


def _session_id(payload: dict, env: dict) -> str:
    explicit = (env.get("ATS_SESSION_ID") or "").strip()
    if explicit:
        return explicit
    cid = (payload.get("session_id") or env.get("CLAUDE_CODE_SESSION_ID") or "").strip()
    if not cid:
        return ""
    from ai_team_sync import session_pointer as sp
    return sp.resolve_pointer(cid, allow_global=False) or ""


def build_activity(payload: dict, env: dict) -> dict | None:
    """Only a client-reported file action with a resolved ATS session is recorded."""
    tool = payload.get("tool_name")
    if tool not in EDIT_TOOLS | READ_TOOLS:
        return None
    file_path = (payload.get("tool_input") or {}).get("file_path")
    if not file_path or _is_noise(file_path):
        return None
    sid = _session_id(payload, env)
    if not sid:
        return None
    return {"session_id": sid, "action": "read" if tool in READ_TOOLS else "edit",
            "path": _display_path(file_path, payload.get("cwd") or env.get("PWD")),
            "repo_root": _roots(file_path)[1] or ""}


def _agent_label(payload: dict, env: dict) -> str:
    """Per-session agent label so two sessions of the same developer stay distinct in
    presence (mirrors mcp.server.session_agent_label). Session token comes from the
    PostToolUse payload's session_id (always present), so whos_editing's exclude_agent
    can omit exactly my session and still surface a parallel same-developer session."""
    base = (env.get("ATS_AGENT") or "claude-code").strip()
    sid = (payload.get("session_id") or env.get("CLAUDE_CODE_SESSION_ID")
           or env.get("ATS_SESSION") or "").strip()
    return f"{base}:{sid[:8]}" if sid else base


def main() -> None:
    try:
        payload = json.load(sys.stdin)
    except Exception:
        sys.exit(0)  # no/!json payload — never block the edit

    env = dict(os.environ)
    body = build_presence(payload, env)
    activity = build_activity(payload, env)
    if body is None and activity is None:
        sys.exit(0)

    server = os.environ.get("ATS_SERVER_URL", "http://localhost:8400")
    try:
        import httpx
        with httpx.Client(timeout=2) as client:
            if activity:
                from ai_team_sync import session_pointer as sp
                cid = payload.get("session_id") or env.get("CLAUDE_CODE_SESSION_ID")
                token = sp.load_approval_token(activity["session_id"], cid)
                if token:
                    client.post(f"{server}/api/file-activities", json=activity,
                                headers={"X-ATS-Approval-Token": token})
            if body:
                client.post(f"{server}/api/presence", json=body)
    except Exception:
        pass  # server down / network — fire-and-forget, never block
    sys.exit(0)


if __name__ == "__main__":
    main()
