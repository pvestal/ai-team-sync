#!/usr/bin/env python3
"""Post-commit hook: logs the commit to the active session."""

from __future__ import annotations

import os
import subprocess
import sys

import httpx

SERVER = os.environ.get("ATS_SERVER_URL", "http://localhost:8400")
# Resolved through session_pointer so $ATS_STATE_DIR isolates a process
# that was launched with its own state directory (a delegated child). A
# hardcoded ~ path would reach into the parent's pointers regardless.
def _session_file() -> str:
    try:
        from ai_team_sync import session_pointer as sp
        return str(sp.global_pointer_path())
    except Exception:
        return os.path.expanduser("~/.ats_session")


def main():
    # Read active session
    if not os.path.exists(_session_file()):
        sys.exit(0)
    with open(_session_file()) as f:
        session_id = f.read().strip()
    if not session_id:
        sys.exit(0)

    # Get commit info
    try:
        hash_result = subprocess.run(
            ["git", "rev-parse", "HEAD"], capture_output=True, text=True, check=True
        )
        msg_result = subprocess.run(
            ["git", "log", "-1", "--pretty=%s"], capture_output=True, text=True, check=True
        )
    except subprocess.CalledProcessError:
        sys.exit(0)

    commit_hash = hash_result.stdout.strip()
    message = msg_result.stdout.strip()

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
