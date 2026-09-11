"""Git integration utilities for change detection."""

from __future__ import annotations

import os
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


def _shared_root_from_gitfile(gitfile: str, worktree_root: str) -> str:
    """The repo a LINKED WORKTREE belongs to; `worktree_root` for anything else.

    A `.git` file also appears in submodules ('gitdir: ../.git/modules/<name>'),
    which are their own project — only the '/.git/worktrees/' form is a second
    checkout of one repo.
    """
    try:
        with open(gitfile, encoding="utf-8", errors="replace") as fh:
            text = fh.read(4096)
    except OSError:
        return worktree_root

    gitdir = ""
    for line in text.splitlines():
        if line.strip().startswith("gitdir:"):
            gitdir = line.split(":", 1)[1].strip()
            break
    if not gitdir:
        return worktree_root

    if not os.path.isabs(gitdir):
        gitdir = os.path.join(worktree_root, gitdir)
    gitdir = os.path.normpath(gitdir)

    marker = f"{os.sep}.git{os.sep}worktrees{os.sep}"
    idx = gitdir.find(marker)
    return gitdir[:idx] if idx > 0 else worktree_root


def resolve_repo_roots(path: str | Path) -> tuple[str | None, str | None]:
    """(worktree_root, repo_root) for `path` — worktree-aware, no subprocess.

    A linked worktree's `.git` is a FILE, not a directory, so walking up for a
    `.git` DIRECTORY sails past the worktree root to the nearest ancestor that
    has one. Edits made from a worktree then reported paths prefixed with the
    worktree's directory name and anchored to an unrelated repo, so the lock
    guard found no owner and the claim guard never fired (observed 2026-09-11).

    `worktree_root` is what paths are made relative to, so one file has one key
    in every checkout. `repo_root` is the SHARED root, so locks and the
    coordinated-repo gate bind across a project's worktrees. Pure path work —
    this runs in a PreToolUse hook on every edit.
    """
    d = os.path.dirname(os.path.abspath(os.fspath(path)))
    while True:
        dot_git = os.path.join(d, ".git")
        if os.path.isdir(dot_git):
            return d, d
        if os.path.isfile(dot_git):
            return d, _shared_root_from_gitfile(dot_git, d)
        parent = os.path.dirname(d)
        if parent == d:
            return None, None
        d = parent
