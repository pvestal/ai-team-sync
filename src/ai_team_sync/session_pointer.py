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

import json
import os
import time
from pathlib import Path

GLOBAL_FILE_NAME = ".ats_session"

# Live-cid handoff (Tower #2003). A Claude process spawns the stdio MCP server
# ONCE; /clear (and resume/compact) then rotates CLAUDE_CODE_SESSION_ID for every
# newly-spawned hook while the long-lived MCP child keeps its spawn-time value.
# The two disagree for the rest of that process's life, which made the lock guard
# block its own session. The owning Claude PID is the one key both sides can
# still agree on, so the SessionStart hook publishes the live cid under it.
LIVE_CID_PREFIX = ".ats_live_cid_"
_LIVE_CID_MAX_AGE_S = 7 * 24 * 3600


def _state_dir() -> Path:
    return Path(os.environ.get("ATS_STATE_DIR") or Path.home())


def env_claude_session_id() -> str | None:
    """This PROCESS's CLAUDE_CODE_SESSION_ID, straight from the environment.

    Correct for anything spawned per-turn (hooks). NOT correct for the stdio MCP
    server, which is spawned once and keeps a pre-/clear value — use
    claude_session_id() there. The SessionStart hook must publish from THIS
    function, or it would republish whatever it read back from its own record.
    """
    cid = (os.environ.get("CLAUDE_CODE_SESSION_ID")
           or os.environ.get("ATS_SESSION") or "").strip()
    return cid or None


def host_pid() -> str:
    """The owning Claude process id — the identifier a hook and the stdio MCP
    subprocess still agree on after /clear rotates CLAUDE_CODE_SESSION_ID.

    Hooks carry it in $CLAUDE_PID. The MCP subprocess does not, but Claude Code
    spawns it directly, so os.getppid() is the same number (verified on tower:
    ats-mcp pid 1891887 -> ppid 1891656 == CLAUDE_PID of the owning session).
    """
    return (os.environ.get("CLAUDE_PID") or "").strip() or str(os.getppid())


def _proc_starttime(pid: str) -> str | None:
    """Host process start-time, to tell a live pid from a recycled one.

    Linux only (field 22 of /proc/<pid>/stat, read after the comm field so a
    process name containing ')' cannot shift the offsets). Returns None where
    /proc is unavailable — then publish records no start-time and the recycle
    check is skipped rather than always firing.
    """
    try:
        with open(f"/proc/{int(pid)}/stat", encoding="utf-8") as fh:
            data = fh.read()
        return data.rsplit(")", 1)[1].split()[19]
    except Exception:
        return None


def live_cid_path(pid: str | None = None) -> Path:
    return _state_dir() / f"{LIVE_CID_PREFIX}{pid or host_pid()}"


def publish_live_cid(cid: str, pid: str | None = None) -> None:
    """Record the LIVE Claude session id for this Claude process.

    Called by the SessionStart hook, which is respawned on /clear and therefore
    always holds the current cid. Best-effort: never raises into a hook."""
    cid = (cid or "").strip()
    if not cid:
        return
    pid = str(pid or host_pid())
    try:
        _state_dir().mkdir(parents=True, exist_ok=True)
    except Exception:
        pass
    try:
        live_cid_path(pid).write_text(json.dumps(
            {"cid": cid, "pid": pid, "starttime": _proc_starttime(pid) or ""}))
    except Exception:
        pass


def live_claude_session_id() -> str | None:
    """The cid most recently published by a hook of THIS Claude process.

    Ignored when the record is older than a week, or when the host pid's
    start-time no longer matches what was recorded — a recycled pid must not let
    a new session inherit a dead one's identity.
    """
    pid = host_pid()
    path = live_cid_path(pid)
    try:
        record = json.loads(path.read_text())
    except Exception:
        return None
    try:
        if time.time() - path.stat().st_mtime > _LIVE_CID_MAX_AGE_S:
            return None
    except Exception:
        pass
    recorded = str(record.get("starttime") or "")
    if recorded and _proc_starttime(pid) != recorded:
        return None  # pid recycled, or the host process is gone
    return str(record.get("cid") or "").strip() or None


def claude_session_id() -> str | None:
    """The host agent's per-session id, as every ATS caller should see it.

    Prefers the cid a hook of this Claude process published, because the stdio
    MCP server's own environment goes stale at the first /clear (#2003). Falls
    back to this process's environment when nothing was published, so agents
    without the SessionStart hook behave exactly as before.
    """
    return live_claude_session_id() or env_claude_session_id()


def detect_agent() -> str:
    """Detect which AI agent is active. The ONE detection (#2517).

    Honors ATS_AGENT (explicit override) first, then known env signatures.
    NOTE: Claude Code sets CLAUDECODE (no underscore) — the old
    CLAUDE_CODE-only check returned 'unknown' (#1556). cli._detect_agent and
    mcp.server.detect_agent delegate here; before #2517 the CLI carried a
    rotted copy that still missed CLAUDECODE, so `ats session start` from a
    Claude Code shell registered as 'unknown'.
    """
    explicit = (os.environ.get("ATS_AGENT") or "").strip()
    if explicit:
        return explicit
    if os.environ.get("CLAUDECODE") or os.environ.get("CLAUDE_CODE"):
        return "claude-code"
    if any(key.startswith("CODEX") for key in os.environ):
        return "codex"
    if os.environ.get("CURSOR_SESSION"):
        return "cursor"
    if os.environ.get("COPILOT_WORKSPACE"):
        return "copilot-workspace"
    return "unknown"


def agent_label(base: str | None = None, cid: str | None = None) -> str:
    """Per-session agent identity: '<base>:<cid8>' (#1556/#2003/#2517).

    THE label the lock guard attributes sessions by: it string-matches the
    hook payload's live cid against this. Every registrar (SessionStart
    autostart, the stdio MCP, the CLI) must build it HERE — a registrar that
    writes a bare base label creates a session its own creator can never be
    matched to, so the guard names the caller's scope as "another ACTIVE
    session's scope" and every re-register adds another blocker (#2517).

    base defaults to detect_agent(); cid defaults to claude_session_id().
    Falls back to the bare base when no cid is known ('unknown' never gets a
    suffix — a token on an unidentified agent reads as identity it isn't).
    """
    base = (base or detect_agent()).strip() or "unknown"
    cid = (cid if cid is not None else (claude_session_id() or "")).strip()
    return f"{base}:{cid[:8]}" if (cid and base != "unknown") else base


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


def resolve_pointer(cid: str | None = None, allow_global: bool = True) -> str | None:
    """Resolve THIS session's ATS session id. See module docstring for order.

    allow_global=False drops step 3 (the legacy shared file). Callers that
    ADOPT the resolved row must pass False: the global file names whichever
    session wrote it last, so a new session would inherit that row's agent
    label, scope and description.
    """
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
    if not allow_global:
        return None
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
