#!/usr/bin/env python3
"""SessionStart hook: AUTO-REGISTER this Claude session with ATS — no manual
start_session required.

The gap this closes: the presence/heartbeat/lock-guard hooks only MAINTAIN a
session that was created manually with start_session. A session that never called
it held no row, so it was invisible to team_status and held no advisory locks —
exactly how a live session can edit shared files while team_status reports an
empty team. This hook creates a lightweight, scope-less session row on
SessionStart and records a per-session pointer (the Gap 3 fix) so the heartbeat
and complete verbs act on the right session.

Scope-less by design: auto-registration ANNOUNCES presence (so team_status and
whos_editing see you) but claims NO locks. Declare real scope when you mean to —
start_session / extend_scope still create locks on top of this row.

Idempotent: SessionStart re-fires on resume and compaction; those reuse the
existing active session instead of spawning duplicates. Fail-open: any error
exits 0 and the session simply falls back to the old manual behavior.

Wire (~/.claude/settings.json):
  "SessionStart": [{ "hooks": [{ "type": "command",
    "command": "<ats-venv>/bin/python -m ai_team_sync.hooks.session_autostart" }] }]
"""
from __future__ import annotations

import asyncio
import os
import subprocess
import sys

from ai_team_sync import session_pointer as sp


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


def _agent_label(cid: str) -> str:
    # Mirrors mcp.server.session_agent_label so auto- and manual-registered rows
    # carry the same identity for a given session.
    return f"claude-code:{cid[:8]}"


async def ensure_session(server_url: str, client) -> str | None:
    """Create-or-reuse this Claude session's ATS row. Returns the ATS session id,
    or None if it can't (no session id / server unreachable) so the caller fails
    open. `client` is an httpx.AsyncClient (real, or ASGI-routed in tests)."""
    # env_ not claude_: this hook is the PUBLISHER of the live cid, and it is
    # respawned on /clear, so its environment is the authority. Reading through
    # claude_session_id() would hand back the pre-/clear value it published last
    # time and the rotation would never propagate (#2003).
    cid = sp.env_claude_session_id()
    if not cid:
        return None  # no stable key → can't dedupe a pointer; leave to manual flow

    # Hand the live cid to this Claude process's stdio MCP server, whose own
    # CLAUDE_CODE_SESSION_ID froze at spawn time and cannot see the rotation.
    sp.publish_live_cid(cid)

    # Idempotent reuse: a pointer we still recognize as active on the server.
    # allow_global=False: the legacy shared pointer names the last session to
    # write it, so reusing it makes THIS session inherit that row's identity.
    existing = sp.resolve_pointer(cid, allow_global=False)
    if existing:
        try:
            r = await client.get(f"{server_url}/api/sessions/{existing}")
            if r.status_code == 200 and r.json().get("status") == "active":
                sp.save_pointer(existing, cid)
                return existing
        except Exception:
            pass  # fall through to create a fresh row

    try:
        # repo_root deliberately NOT sent: on multi-repo boxes the Claude cwd
        # (~/Documents) is not the repo the session will lock, and a wrong
        # anchor turns enforcement OFF for that repo (false negative — worse
        # than the false positive). Anchoring is opt-in via start_session.
        r = await client.post(f"{server_url}/api/sessions", json={
            "developer": _developer(),
            "agent": _agent_label(cid),
            "scope": [],
            "description": "auto-registered on SessionStart",
            "auto_lock": False,
        })
        if r.status_code in (200, 201):
            sid = r.json()["id"]
            sp.save_pointer(sid, cid)
            return sid
    except Exception:
        pass
    return None


def main() -> None:
    server = os.environ.get("ATS_SERVER_URL", "http://localhost:8400")

    async def _run() -> str | None:
        import httpx
        async with httpx.AsyncClient(timeout=3) as c:
            return await ensure_session(server, c)

    try:
        sid = asyncio.run(_run())
        if sid:
            # SessionStart hook stdout is surfaced as session context.
            print(f"[ats] session auto-registered ({sid[:8]}) — visible in team_status; "
                  f"declare scope with start_session/extend_scope to claim locks")
    except Exception:
        pass
    sys.exit(0)  # always fail-open: never wedge session startup


if __name__ == "__main__":
    main()
