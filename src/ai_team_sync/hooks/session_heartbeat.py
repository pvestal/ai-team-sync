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
    # Per-session pointer (keyed by CLAUDE_CODE_SESSION_ID) bumps THIS session even
    # when a concurrent session clobbered the legacy global file (Gap 3). Resolution
    # order — $ATS_SESSION_ID, per-session file, global file — lives in one place.
    try:
        from ai_team_sync import session_pointer as sp
        return sp.resolve_pointer()
    except Exception:
        # Fail-open fallback to the legacy behavior if the module can't import.
        sid = (os.environ.get("ATS_SESSION_ID") or os.environ.get("ATS_SESSION") or "").strip()
        if sid:
            return sid
        try:
            return SESSION_FILE.read_text().strip() or None
        except Exception:
            return None


def _throttled(session_id: str) -> bool:
    """True when a beat went out recently enough to skip this one.

    Wiring this hook at PostToolUse (2026-08-17) is what keeps a LONG turn
    alive — the reaper's ~20m timeout is shorter than a heavy render/review
    turn, and Stop only fires at turn END, so sessions were being reaped
    mid-turn with their locks silently released. Per-tool-call beats need a
    throttle: mtime of a tiny state file, one beat per interval, so a
    tool-dense turn does not hammer the server."""
    try:
        interval = int(os.environ.get("ATS_HEARTBEAT_MIN_INTERVAL_S", "60"))
    except ValueError:
        interval = 60
    if interval <= 0:
        return False
    marker = Path(os.environ.get("ATS_STATE_DIR") or Path.home()) / f".ats_hb_{session_id[:8]}"
    try:
        import time
        if marker.exists() and (time.time() - marker.stat().st_mtime) < interval:
            return True
        marker.touch()
    except Exception:
        pass  # throttle bookkeeping must never stop a beat
    return False


def main() -> None:
    session_id = _resolve_session_id()
    if not session_id:
        sys.exit(0)  # no active ATS session — nothing to heartbeat
    if _throttled(session_id):
        sys.exit(0)
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
