#!/usr/bin/env python3
"""Post-checkout hook: auto-detect session start based on config."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import httpx

SERVER = os.environ.get("ATS_SERVER_URL", "http://localhost:8400")
SESSION_FILE = os.path.expanduser("~/.ats_session")


def load_config() -> dict:
    """Load team config from .ai-team-sync.toml."""
    try:
        import tomllib
    except ImportError:
        import tomli as tomllib

    config_file = Path.cwd() / ".ai-team-sync.toml"
    if not config_file.exists():
        return {}

    with open(config_file, "rb") as f:
        return tomllib.load(f)


def get_developer() -> str:
    """Get developer name from git config."""
    try:
        result = subprocess.run(
            ["git", "config", "user.name"],
            capture_output=True, text=True, check=True,
        )
        return result.stdout.strip()
    except subprocess.CalledProcessError:
        return "unknown"


def get_branch() -> str:
    """Get current branch name."""
    try:
        result = subprocess.run(
            ["git", "rev-parse", "--abbrev-ref", "HEAD"],
            capture_output=True, text=True, check=True,
        )
        return result.stdout.strip()
    except subprocess.CalledProcessError:
        return ""


def detect_agent() -> str:
    """Detect which AI agent is active from environment."""
    if os.environ.get("CLAUDE_CODE"):
        return "claude-code"
    if os.environ.get("CURSOR_SESSION"):
        return "cursor"
    if os.environ.get("COPILOT_WORKSPACE"):
        return "copilot-workspace"
    return "unknown"


def has_active_session() -> bool:
    """Check if there's already an active session."""
    if not os.path.exists(SESSION_FILE):
        return False

    try:
        with open(SESSION_FILE) as f:
            session_id = f.read().strip()

        # Verify session is still active on server
        with httpx.Client(timeout=5) as client:
            resp = client.get(f"{SERVER}/api/sessions/{session_id}")
            if resp.status_code == 200:
                data = resp.json()
                return data.get("status") == "active"
    except Exception:
        pass

    return False


def auto_start_session(branch: str, config: dict):
    """Auto-start a session based on branch patterns."""
    session_config = config.get("session", {})

    # Determine scope based on branch
    # This is a simple heuristic - teams can customize this
    scope = ["**/*"]  # Default: entire repo
    description = f"Working on {branch}"

    # You could add custom branch → scope mapping here
    # For example:
    # if branch.startswith("feature/"):
    #     scope = ["src/**"]
    # elif branch.startswith("docs/"):
    #     scope = ["docs/**"]

    try:
        with httpx.Client(timeout=10) as client:
            resp = client.post(f"{SERVER}/api/sessions", json={
                "developer": get_developer(),
                "agent": detect_agent(),
                "scope": scope,
                "description": description,
                "branch": branch,
                "auto_lock": session_config.get("auto_lock", True),
                "lock_mode": config.get("locks", {}).get("default_mode", "advisory"),
            })

            if resp.status_code == 201:
                data = resp.json()
                # Save session ID
                with open(SESSION_FILE, "w") as f:
                    f.write(data["id"])

                print(f"\n[ai-team-sync] Auto-started session for branch '{branch}'")
                print(f"  Session ID: {data['id'][:8]}...")
                print(f"  Scope: {', '.join(scope)}")
                if data.get("lock_count"):
                    print(f"  Locks created: {data['lock_count']}")
                print()
    except Exception as e:
        # Silently fail - don't block git operations
        pass


def main():
    """Main hook entry point."""
    # Git passes: previous_head current_head is_branch_checkout
    if len(sys.argv) < 4:
        sys.exit(0)

    previous_head = sys.argv[1]
    current_head = sys.argv[2]
    is_branch_checkout = sys.argv[3] == "1"

    # Only auto-start on branch checkouts (not file checkouts)
    if not is_branch_checkout:
        sys.exit(0)

    # Load config
    config = load_config()
    session_config = config.get("session", {})

    # Check if auto-detection is enabled
    if not session_config.get("auto_detect_agent", False):
        sys.exit(0)

    # Don't auto-start if there's already an active session
    if has_active_session():
        sys.exit(0)

    # Auto-start session for new branch
    branch = get_branch()
    if branch and branch not in ("HEAD", "main", "master"):
        auto_start_session(branch, config)

    sys.exit(0)


if __name__ == "__main__":
    main()
