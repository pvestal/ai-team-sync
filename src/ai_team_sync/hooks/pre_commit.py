#!/usr/bin/env python3
"""Pre-commit hook: refuse a commit whose staged paths another live ATS session
has claimed (#3581 part B). Rules and limits: hooks/commit_guard.py and
docs/commit-guard.md.

Before #3581 this hook posted no repo_root (so a relative path matched every
repository's locks), identified no committing session (so a session's own
exclusive lock blocked its own commit) and allowed every commit when ATS was
down. It now sends the canonical repo root, excludes the committer's own locks,
and fails closed in governed repositories.

A conflicted merge is concluded by `git commit`, which runs THIS hook with
MERGE_HEAD present; the merged side's changed paths are added to the staged
set then. A clean merge runs pre-merge-commit instead.

Bypasses (documented, not hidden): `git commit --no-verify`, or
ATS_COMMIT_GUARD_OVERRIDE=<reason>, which is printed and appended to the local
audit log (commit_guard.audit_log_path()).
"""

from __future__ import annotations

import sys

from ai_team_sync.hooks import commit_guard as cg


def main() -> None:
    try:
        paths = set(cg.staged_paths())
    except cg.GuardUnavailable as exc:
        print(f"{cg.TAG} cannot list staged paths: {exc}", file=sys.stderr)
        sys.exit(1)
    paths.update(cg.merge_incoming_paths())
    sys.exit(cg.run_git_guard("pre-commit", sorted(paths)))


if __name__ == "__main__":
    main()
