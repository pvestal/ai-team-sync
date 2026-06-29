#!/usr/bin/env python3
"""Client heartbeat hook: prove this session is still ALIVE to the ATS reaper.

The reaper (background_tasks.auto_complete_stale_sessions) gives sessions that
heartbeat a FAST cleanup path: go silent for session_heartbeat_timeout_minutes
(~20m) and your locks are reclaimed, instead of lingering the full
session_inactivity_hours fallback window after your process dies. This hook
supplies that heartbeat.

Wire it tool-AGNOSTICALLY so reads/bash-only turns still count as alive — an
edit-only signal would falsely reap a genuinely-active read-heavy session (the
exact reason an edit-only heartbeat was rejected; see the Gap 1 doc). The Stop
hook fires once at the end of EVERY assistant turn regardless of tools used, so
it is the right trigger; add UserPromptSubmit too for an extra bump.

Wire (~/.claude/settings.json):
  "Stop": [{ "hooks": [{ "type": "command",
    "command": "<ats-venv>/bin/python -m ai_team_sync.hooks.session_heartbeat" }] }],
  "UserPromptSubmit": [{ "hooks": [{ "type": "command",
    "command": "<ats-venv>/bin/python -m ai_team_sync.hooks.session_heartbeat" }] }]

Which session is bumped: ATS_SESSION_ID / ATS_SESSION env (preferred — survives
concurrent sessions), else ~/.ats_session (single global file written by the MCP
server; with concurrent Claude sessions only the most-recent start_session is
recorded there — known limitation, see Gap 3 in the product-gaps doc).

Always fail-OPEN and exit 0: a heartbeat is best-effort and must never wedge or
slow a turn. Errors are swallowed; a short timeout caps latency.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

SESSION_FILE = Path.home() / ".ats_session"


def _resolve_session_id() -> str | None:
    sid = (os.environ.get("ATS_SESSION_ID") or os.environ.get("ATS_SESSION") or "").strip()
    if sid:
        return sid
    try:
        content = SESSION_FILE.read_text().strip()
        return content or None
    except Exception:
        return None


def main() -> None:
    session_id = _resolve_session_id()
    if not session_id:
        sys.exit(0)  # no active ATS session — nothing to heartbeat
    server = os.environ.get("ATS_SERVER_URL", "http://localhost:8400")
    try:
        import httpx
        with httpx.Client(timeout=2) as client:
            client.post(f"{server}/api/sessions/{session_id}/heartbeat")
    except Exception:
        pass  # best-effort: server down / network / 404 — never block the turn
    sys.exit(0)


if __name__ == "__main__":
    main()
