#!/usr/bin/env python3
"""PreToolUse hook: ATS lock READ-GUARD — the missing half of coordination.

The PostToolUse presence hook BROADCASTS what I edit. Nothing made an agent
READ who owns a file before editing it, so "do not clobber" was purely manual
and silently failed (a parallel session's locked file could be overwritten with
zero warning). This closes that: before an Edit/Write/MultiEdit, it asks the ATS
server which OTHER active sessions declared scope over the target file, and
BLOCKS the edit (exit 2, reason on stderr) when one does — excluding my own
session via the hook payload's session_id so I never block myself.

Fail-OPEN: any error (server down, bad payload, no scope data) exits 0 and lets
the edit proceed — coordination must never wedge real work. Set
ATS_LOCKCHECK_BLOCK=0 to downgrade from block to warn-only.

Wire (~/.claude/settings.json):
  "PreToolUse": [{ "matcher": "Edit|Write|MultiEdit|NotebookEdit",
    "hooks": [{ "type": "command",
      "command": "<ats-venv>/bin/python <this-file>" }] }]
"""
from __future__ import annotations

import fnmatch
import json
import os
import sys

EDIT_TOOLS = {"Edit", "Write", "MultiEdit", "NotebookEdit"}
_SKIP_SUBSTR = ("/.git/", "/node_modules/", "/__pycache__/", "/.venv/",
                "/scratchpad/", "/.claude/", "/.playwright-mcp/")
_SKIP_PREFIX = ("/tmp/", "/var/tmp/", "/private/tmp/")


def _is_noise(path: str) -> bool:
    if any(path.startswith(p) for p in _SKIP_PREFIX):
        return True
    return any(s in path for s in _SKIP_SUBSTR)


def _git_root(path: str) -> str | None:
    d = os.path.dirname(os.path.abspath(path))
    while True:
        if os.path.isdir(os.path.join(d, ".git")):
            return d
        parent = os.path.dirname(d)
        if parent == d:
            return None
        d = parent


def _rel(path: str) -> str:
    root = _git_root(path)
    if root:
        try:
            return os.path.relpath(path, root)
        except Exception:
            pass
    return os.path.basename(path)


def scope_matches(rel: str, pattern: str) -> bool:
    """True if repo-relative `rel` falls under a scope `pattern`. Supports the
    '**' = any-depth convention ATS scopes use (e.g. 'packages/scene_generation/**')."""
    pat = (pattern or "").strip().rstrip("/")
    if not pat:
        return False
    if pat.endswith("/**"):
        base = pat[:-3]
        return rel == base or rel.startswith(base + "/")
    if pat.endswith("/*"):
        base = pat[:-2]
        return os.path.dirname(rel) == base
    return fnmatch.fnmatch(rel, pat)


def find_conflicts(rel: str, sessions: list, my_session_id: str) -> list:
    """OTHER active sessions whose scope covers `rel`. Excludes my own session
    (matched by the session-id prefix the server appends to the agent id)."""
    mine = (my_session_id or "")[:8]
    out = []
    for s in sessions or []:
        if str(s.get("status", "")).lower() != "active":
            continue
        agent = str(s.get("agent", ""))
        if mine and mine in agent:          # my own session — never self-block
            continue
        scope = s.get("scope") or s.get("files") or []
        if isinstance(scope, str):
            scope = [scope]
        for pat in scope:
            if scope_matches(rel, str(pat)):
                out.append((agent, str(s.get("description", ""))[:90], str(pat)))
                break
    return out


def main() -> None:
    try:
        payload = json.load(sys.stdin)
    except Exception:
        sys.exit(0)  # unparseable — never block
    if payload.get("tool_name") not in EDIT_TOOLS:
        sys.exit(0)
    fp = (payload.get("tool_input") or {}).get("file_path")
    if not fp or _is_noise(fp):
        sys.exit(0)
    rel = _rel(fp)

    server = os.environ.get("ATS_SERVER_URL", "http://localhost:8400")
    try:
        import httpx
        with httpx.Client(timeout=2) as client:
            data = client.get(f"{server}/api/sessions").json()
    except Exception:
        sys.exit(0)  # server down / network — fail open
    sessions = data if isinstance(data, list) else data.get("sessions", data.get("data", []))

    conflicts = find_conflicts(rel, sessions, payload.get("session_id", ""))
    if not conflicts:
        sys.exit(0)

    lines = [f"ATS LOCK GUARD: '{rel}' is inside another ACTIVE session's scope — coordinate, do not clobber:"]
    for agent, desc, pat in conflicts:
        lines.append(f"  - {agent}  [{pat}]  {desc}")
    lines.append("Read their work (ats / :8400) and coordinate, or request override. "
                 "Set ATS_LOCKCHECK_BLOCK=0 to downgrade to warn-only.")
    print("\n".join(lines), file=sys.stderr)
    sys.exit(2 if os.environ.get("ATS_LOCKCHECK_BLOCK", "2") != "0" else 0)


if __name__ == "__main__":
    main()
