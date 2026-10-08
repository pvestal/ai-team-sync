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
import sys

from ai_team_sync import session_pointer as sp
from ai_team_sync.hooks.session_registration import RegistrationInput
from ai_team_sync.hooks.session_registration import ensure_session as ensure_registered_session


def _agent_label(cid: str) -> str:
    """Backward-compatible Claude label helper; the SSOT remains session_pointer."""
    return sp.agent_label("claude-code", cid)


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

    return await ensure_registered_session(
        server_url,
        client,
        RegistrationInput(
            lifecycle_session_id=cid,
            agent="claude-code",
            cwd=os.getcwd(),
            hook_event_name="SessionStart",
        ),
    )


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
                  "ATS-first context resolves on the first governed prompt; "
                  "declare file scope with start_session/extend_scope before editing")
    except Exception:
        pass
    sys.exit(0)  # always fail-open: never wedge session startup


if __name__ == "__main__":
    main()
