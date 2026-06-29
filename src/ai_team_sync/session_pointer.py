"""Per-Claude-session ATS pointer files — the Gap 3 fix.

Background: the MCP server persisted "the active session id" to a single global
file (~/.ats_session). Two concurrent Claude sessions on one machine clobbered
each other's pointer — last start_session wins — so the heartbeat hook and the
MCP verbs that read it (complete_session, log_decision, request_override) could
act on the WRONG session.

Fix: every Claude session has a stable id in $CLAUDE_CODE_SESSION_ID, present in
every hook's env and in the stdio MCP server's env. We persist that session's ATS
session id to a per-session file keyed by it, so concurrent sessions never
overwrite each other. The legacy global file is still written for back-compat,
but the per-session file always wins on resolve.

Resolution order (resolve_pointer):
  1. $ATS_SESSION_ID  — explicit operator override, always wins.
  2. ~/.ats_session_<cid8>  — this session's own pointer (concurrency-safe).
  3. ~/.ats_session  — legacy global (single-session fallback).

State dir is $ATS_STATE_DIR if set (tests), else $HOME.
"""
from __future__ import annotations

import os
from pathlib import Path

GLOBAL_FILE_NAME = ".ats_session"


def _state_dir() -> Path:
    return Path(os.environ.get("ATS_STATE_DIR") or Path.home())


def claude_session_id() -> str | None:
    """The host agent's stable per-session id. CLAUDE_CODE_SESSION_ID is set by
    Claude Code in every hook + MCP subprocess; ATS_SESSION is the legacy token."""
    cid = (os.environ.get("CLAUDE_CODE_SESSION_ID")
           or os.environ.get("ATS_SESSION") or "").strip()
    return cid or None


def global_pointer_path() -> Path:
    return _state_dir() / GLOBAL_FILE_NAME


def session_pointer_path(cid: str) -> Path:
    return _state_dir() / f"{GLOBAL_FILE_NAME}_{cid[:8]}"


def save_pointer(session_id: str, cid: str | None = None) -> None:
    """Record the ATS session id for this Claude session. Writes both the
    per-session pointer (authoritative) and the global file (back-compat).
    Best-effort: never raises into a hook/turn."""
    cid = cid or claude_session_id()
    sd = _state_dir()
    try:
        sd.mkdir(parents=True, exist_ok=True)
    except Exception:
        pass
    try:
        global_pointer_path().write_text(session_id)
    except Exception:
        pass
    if cid:
        try:
            session_pointer_path(cid).write_text(session_id)
        except Exception:
            pass


def resolve_pointer(cid: str | None = None) -> str | None:
    """Resolve THIS session's ATS session id. See module docstring for order."""
    env = (os.environ.get("ATS_SESSION_ID") or "").strip()
    if env:
        return env
    cid = cid or claude_session_id()
    if cid:
        try:
            content = session_pointer_path(cid).read_text().strip()
            if content:
                return content
        except Exception:
            pass
    try:
        content = global_pointer_path().read_text().strip()
        return content or None
    except Exception:
        return None


def clear_pointer(cid: str | None = None) -> None:
    """Drop this session's per-session pointer (on complete_session). Leaves the
    global file alone — another session may legitimately own it."""
    cid = cid or claude_session_id()
    if not cid:
        return
    try:
        session_pointer_path(cid).unlink()
    except Exception:
        pass
