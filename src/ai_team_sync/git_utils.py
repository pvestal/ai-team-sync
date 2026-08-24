"""Git integration utilities for change detection."""

from __future__ import annotations

import subprocess
from pathlib import Path
from fnmatch import fnmatch


def get_uncommitted_files(repo_path: Path | None = None) -> list[str]:
    """Get list of files with uncommitted changes (staged or unstaged)."""
    if repo_path is None:
        repo_path = Path.cwd()

    try:
        # Get staged files
        result = subprocess.run(
            ["git", "diff", "--cached", "--name-only"],
            cwd=repo_path,
            capture_output=True,
            text=True,
            check=True,
        )
        staged = result.stdout.strip().split("\n") if result.stdout.strip() else []

        # Get unstaged files
        result = subprocess.run(
            ["git", "diff", "--name-only"],
            cwd=repo_path,
            capture_output=True,
            text=True,
            check=True,
        )
        unstaged = result.stdout.strip().split("\n") if result.stdout.strip() else []

        # Get untracked files
        result = subprocess.run(
            ["git", "ls-files", "--others", "--exclude-standard"],
            cwd=repo_path,
            capture_output=True,
            text=True,
            check=True,
        )
        untracked = result.stdout.strip().split("\n") if result.stdout.strip() else []

        # Combine and deduplicate
        all_files = list(set(staged + unstaged + untracked))
        return [f for f in all_files if f]  # Filter empty strings

    except (subprocess.CalledProcessError, FileNotFoundError):
        return []


def get_staged_files(repo_path: Path | None = None) -> list[str]:
    """Get list of files staged for commit."""
    if repo_path is None:
        repo_path = Path.cwd()

    try:
        result = subprocess.run(
            ["git", "diff", "--cached", "--name-only"],
            cwd=repo_path,
            capture_output=True,
            text=True,
            check=True,
        )
        files = result.stdout.strip().split("\n") if result.stdout.strip() else []
        return [f for f in files if f]

    except (subprocess.CalledProcessError, FileNotFoundError):
        return []


def files_match_patterns(files: list[str], patterns: list[str]) -> dict[str, list[str]]:
    """
    Check which files match which patterns.

    Returns dict: {pattern: [matching_files]}
    """
    matches = {}
    for pattern in patterns:
        matching = [f for f in files if fnmatch(f, pattern)]
        if matching:
            matches[pattern] = matching
    return matches


# Cap the per-session list so a huge dirty tree cannot bloat a payload or a
# reaper summary (ats-git-diff-merge-workflow-p01).
UNCOMMITTED_CAP = 20


def uncommitted_for_scope(repo_root: str | None, scope: list[str],
                          cache: dict[str, list[str]] | None = None) -> list[str]:
    """Uncommitted files under `repo_root` that fall inside `scope`.

    STATUS-AGNOSTIC ON PURPOSE (#2554). The session-list view only wants this
    for ACTIVE sessions — recomputing it for a session completed hours ago
    would report whoever is dirty in that repo NOW as if it were that session's
    stranded work. But the reaper needs the same answer for a session it is
    about to complete, and asking after the status flip is too late. So the
    status decision belongs to each caller, and the computation lives here.

    `cache` memoizes the git call per repo_root across one sweep/request —
    sessions frequently share a repo.
    """
    root = (repo_root or "").strip()
    if not root:
        return []
    if cache is None:
        cache = {}
    if root not in cache:
        cache[root] = get_uncommitted_files(Path(root))
    files = cache[root]
    if not files:
        return []
    matched = {f for fl in files_match_patterns(files, scope or []).values() for f in fl}
    return sorted(matched)[:UNCOMMITTED_CAP]


def get_current_branch(repo_path: Path | None = None) -> str:
    """Get current git branch name."""
    if repo_path is None:
        repo_path = Path.cwd()

    try:
        result = subprocess.run(
            ["git", "rev-parse", "--abbrev-ref", "HEAD"],
            cwd=repo_path,
            capture_output=True,
            text=True,
            check=True,
        )
        return result.stdout.strip()
    except (subprocess.CalledProcessError, FileNotFoundError):
        return ""


def get_repo_root(path: Path | None = None) -> Path | None:
    """Get the root directory of the git repository."""
    if path is None:
        path = Path.cwd()

    try:
        result = subprocess.run(
            ["git", "rev-parse", "--show-toplevel"],
            cwd=path,
            capture_output=True,
            text=True,
            check=True,
        )
        return Path(result.stdout.strip())
    except (subprocess.CalledProcessError, FileNotFoundError):
        return None
