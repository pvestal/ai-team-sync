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
