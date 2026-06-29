#!/usr/bin/env python3
"""UserPromptSubmit hook: surface pending override requests INTO the owner's turn.

The override handshake was the one coordination step that still needed active
attention: when another session calls request_override on YOUR lock, nothing pushed
that into your loop — you only saw it if you happened to call check_pending_requests,
or a human relayed the Slack/Telegram alert. Claude Code has no inbound interrupt
mid-session, but UserPromptSubmit fires at the start of every turn and its stdout is
injected into the agent's context. So this hook turns the last poll-only item into a
passive one: each turn, if requests are waiting on YOU, you're told — with the IDs to
respond_to_request.

Owner-only: shows requests where this session is the lock OWNER (incoming), not ones
you sent. Same session resolution as the heartbeat hook: ATS_SESSION_ID / ATS_SESSION
env, else ~/.ats_session.

Wire (~/.claude/settings.json):
  "UserPromptSubmit": [{ "hooks": [{ "type": "command",
    "command": "<ats-venv>/bin/python -m ai_team_sync.hooks.override_inbox" }] }]

Always fail-OPEN and exit 0 (never block a prompt). No requests / server down / error
=> no output, exit 0.
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
        return SESSION_FILE.read_text().strip() or None
    except Exception:
        return None


def format_inbox(requests: list, session_id: str) -> str | None:
    """Render incoming pending override requests (this session as OWNER) into a
    one-block context note, or None if there are none. Pure — unit-testable."""
    incoming = [
        r for r in (requests or [])
        if r.get("owner_session_id") == session_id
        and str(r.get("status", "")).lower() == "pending"
    ]
    if not incoming:
        return None
    lines = [f"ATS: {len(incoming)} override request(s) awaiting YOUR response "
             f"(another session wants to work in a path you locked):"]
    for r in incoming:
        rid = str(r.get("id", ""))[:8]
        who = r.get("requester_developer") or "unknown"
        pat = r.get("conflicting_pattern", "?")
        why = (r.get("justification") or "").strip()
        why = f" — {why[:80]}" if why else ""
        lines.append(f"  - [{rid}] {who} wants '{pat}'{why}")
    lines.append("Respond with respond_to_request (approve/deny), or ignore to let it "
                 "auto-expire (15m).")
    return "\n".join(lines)


def main() -> None:
    session_id = _resolve_session_id()
    if not session_id:
        sys.exit(0)
    server = os.environ.get("ATS_SERVER_URL", "http://localhost:8400")
    try:
        import httpx
        with httpx.Client(timeout=2) as client:
            requests = client.get(
                f"{server}/api/override-requests",
                params={"session_id": session_id, "status": "pending"},
            ).json()
    except Exception:
        sys.exit(0)  # best-effort: never block the prompt
    note = format_inbox(requests if isinstance(requests, list) else [], session_id)
    if note:
        print(note)  # stdout -> injected into the agent's turn context
    sys.exit(0)


if __name__ == "__main__":
    main()
