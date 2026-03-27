#!/usr/bin/env python3
"""Pre-commit hook: warns or blocks if staged files overlap with active scope locks."""

from __future__ import annotations

import os
import subprocess
import sys

import httpx

SERVER = os.environ.get("ATS_SERVER_URL", "http://localhost:8400")


def get_staged_files() -> list[str]:
    result = subprocess.run(
        ["git", "diff", "--cached", "--name-only"],
        capture_output=True, text=True,
    )
    return [f.strip() for f in result.stdout.strip().split("\n") if f.strip()]


def main():
    staged = get_staged_files()
    if not staged:
        sys.exit(0)

    try:
        with httpx.Client(timeout=5) as client:
            resp = client.post(f"{SERVER}/api/locks/check", json={"paths": staged})
            if resp.status_code != 200:
                # Server unreachable — don't block commits
                sys.exit(0)
            results = resp.json()
    except (httpx.ConnectError, httpx.TimeoutException):
        # Server not running — allow commit
        sys.exit(0)

    blocked = []
    warned = []

    for r in results:
        if not r["locked"]:
            continue
        if r["mode"] == "exclusive":
            blocked.append(r)
        else:
            warned.append(r)

    if warned:
        print("\n[ai-team-sync] WARNING — these files overlap with active scope locks:")
        for r in warned:
            print(f"  {r['path']} — locked by {r['developer']} (pattern: {r['pattern']})")
        print()

    if blocked:
        print("\n[ai-team-sync] BLOCKED — these files have exclusive locks:")
        for r in blocked:
            print(f"  {r['path']} — locked by {r['developer']} (pattern: {r['pattern']})")
        print("\nCommit blocked. Coordinate with the lock holder or use: ats lock list")
        sys.exit(1)

    sys.exit(0)


if __name__ == "__main__":
    main()
