#!/usr/bin/env python3
"""Post-commit hook: logs the commit to the active session."""

from __future__ import annotations

import os
import subprocess
import sys

import httpx

SERVER = os.environ.get("ATS_SERVER_URL", "http://localhost:8400")


def _session_id() -> str | None:
    """This agent's own session, or None.

    Recording a commit is a mutation, and commit time counts as liveness for
    the reaper, so the shared ~/.ats_session is refused like every other
    mutation refuses it: it names whichever session on the box wrote it last.
    """
    from ai_team_sync import session_pointer as sp

    session_id, source = sp.resolve_pointer_source()
    return session_id if source in ("env", "per_session") else None


def _head() -> tuple[str, str]:
    hash_result = subprocess.run(
        ["git", "rev-parse", "HEAD"], capture_output=True, text=True, check=True
    )
    msg_result = subprocess.run(
        ["git", "log", "-1", "--pretty=%s"], capture_output=True, text=True, check=True
    )
    return hash_result.stdout.strip(), msg_result.stdout.strip()


def main():
    session_id = _session_id()
    if not session_id:
        sys.exit(0)

    try:
        commit_hash, message = _head()
    except subprocess.CalledProcessError:
        sys.exit(0)

    try:
        with httpx.Client(timeout=5) as client:
            client.post(f"{SERVER}/api/sessions/{session_id}/commits", json={
                "session_id": session_id,
                "commit_hash": commit_hash,
                "message": message,
            })
    except (httpx.ConnectError, httpx.TimeoutException):
        pass  # Don't interfere with commits if server is down

    sys.exit(0)


if __name__ == "__main__":
    main()
