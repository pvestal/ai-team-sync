#!/usr/bin/env python3
"""Prepare-commit-msg hook: appends session context to commit messages."""

from __future__ import annotations

import os
import sys

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
    if len(sys.argv) < 2:
        sys.exit(0)

    commit_msg_file = sys.argv[1]

    # Only enrich regular commits, not merges/squashes
    if len(sys.argv) > 2 and sys.argv[2] in ("merge", "squash"):
        sys.exit(0)

    # Check for active session
    if not os.path.exists(_session_file()):
        sys.exit(0)
    with open(_session_file()) as f:
        session_id = f.read().strip()
    if not session_id:
        sys.exit(0)

    # Append session reference to commit message
    with open(commit_msg_file, "a") as f:
        f.write(f"\n\nai-team-sync-session: {session_id}")

    sys.exit(0)


if __name__ == "__main__":
    main()
