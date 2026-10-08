#!/usr/bin/env python3
"""Codex SessionStart adapter for shared ATS lifecycle registration.

Codex supplies the lifecycle identity on stdin. No Claude environment variable
is read or required. The adapter talks directly to local ATS REST because Codex
may run SessionStart before its MCP clients are initialized.
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
from typing import Any

from ai_team_sync.hooks.session_registration import RegistrationInput, ensure_session


def registration_input(payload: dict[str, Any]) -> RegistrationInput:
    return RegistrationInput(
        lifecycle_session_id=str(payload.get("session_id") or ""),
        agent="codex",
        cwd=str(payload.get("cwd") or os.getcwd()),
        hook_event_name=str(payload.get("hook_event_name") or "SessionStart"),
        model=str(payload.get("model") or ""),
        source=str(payload.get("source") or ""),
    )


def _output(context: str) -> None:
    print(
        json.dumps(
            {
                "hookSpecificOutput": {
                    "hookEventName": "SessionStart",
                    "additionalContext": context,
                }
            }
        )
    )


def main() -> None:
    try:
        payload = json.loads(sys.stdin.read())
        if not isinstance(payload, dict):
            payload = {}
    except Exception:  # noqa: BLE001
        payload = {}
    data = registration_input(payload)
    server = os.environ.get("ATS_SERVER_URL", "http://localhost:8400")

    async def _run() -> str | None:
        import httpx

        async with httpx.AsyncClient(timeout=3) as client:
            return await ensure_session(server, client, data)

    try:
        sid = asyncio.run(_run())
    except Exception:  # noqa: BLE001 - SessionStart must not wedge Codex startup
        sid = None
    if sid:
        _output(
            f"[ats] Codex lifecycle is registered as ATS session {sid}. "
            "The session is unscoped unless cwd is governed; the first governed "
            "prompt must resolve ATS authority before Echo or live verification."
        )
    else:
        # This is deliberately visible context, not a silent permanent bypass.
        # UserPromptSubmit retries direct registration for governed prompts.
        _output(
            "[ats] SessionStart registration was deferred because local ATS was "
            "unavailable or no Codex session_id was supplied. Governed prompts "
            "must retry ATS registration and fail closed if context is unavailable."
        )
    raise SystemExit(0)


if __name__ == "__main__":
    main()
