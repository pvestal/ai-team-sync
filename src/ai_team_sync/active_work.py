"""Unfinished branches that already touch a piece of work (#3591).

WHY THIS EXISTS
---------------
ATS locks say who holds a file RIGHT NOW. They expire; branches do not. A
reviewed fix can sit on a branch for weeks with no lock, and the next session
to touch the same files sees nothing: ATS reported none of the dozen unmerged
branches that modified the same prompt module while a new package of work on
that module was starting.

This module answers, read-only from git: which local branches carry patches
not yet on the default branch, which of them change files in a given scope,
which ticket each names, which worktree (and, when the worktree lifecycle has
locked it, which session) holds it, and whether it actually conflicts with the
default branch or merely touches the same file.

It never blocks. Same-file overlap is information; the caller decides.
"""

from __future__ import annotations

import re
import subprocess
from dataclasses import asdict, dataclass, field
from pathlib import Path

from ai_team_sync.scope_paths import patterns_overlap

GIT_TIMEOUT_S = 10
MAX_BRANCHES_REPORTED = 12

_TICKET_IN_NAME = re.compile(r"(?:^|[/_-])(\d{3,5})(?=[-_/]|$)")
_TICKET_IN_SUBJECT = re.compile(r"#(\d{3,5})\b")
# The worktree lifecycle (#3575) locks managed worktrees with a reason of this
# shape; read it when present, never write it.
_LOCK_SESSION = re.compile(r"\bsession=([0-9a-fA-F-]{6,36})")


@dataclass
class BranchWork:
    branch: str
    tickets: list[int]
    unique_commits: int
    last_commit: str
    files_in_scope: list[str]
    worktree: str = ""
    worktree_session: str = ""
    conflict: str = "unknown"  # merges_cleanly | conflicts_with_default | unknown
    conflict_files: list[str] = field(default_factory=list)

    def as_dict(self) -> dict:
        return asdict(self)


def _git(root: str, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["git", "-C", root, *args], capture_output=True, text=True,
                          timeout=GIT_TIMEOUT_S)


def default_branch(root: str) -> str:
    for name in ("main", "master"):
        if _git(root, "rev-parse", "--verify", "--quiet", f"refs/heads/{name}").returncode == 0:
            return name
    return ""


def _worktrees(root: str) -> dict[str, tuple[str, str]]:
    """branch -> (worktree path, session from a lifecycle lock reason)."""
    out = _git(root, "worktree", "list", "--porcelain")
    found: dict[str, tuple[str, str]] = {}
    path = branch = reason = ""
    for line in out.stdout.splitlines() + [""]:
        if line.startswith("worktree "):
            path = line[len("worktree "):]
        elif line.startswith("branch refs/heads/"):
            branch = line[len("branch refs/heads/"):]
        elif line.startswith("locked"):
            reason = line[len("locked"):].strip()
        elif not line:
            if branch:
                match = _LOCK_SESSION.search(reason)
                found[branch] = (path, match.group(1) if match else "")
            path = branch = reason = ""
    return found


def _tickets(branch: str, subjects: list[str]) -> list[int]:
    ids = {int(m) for m in _TICKET_IN_NAME.findall(branch)}
    for subject in subjects:
        ids.update(int(m) for m in _TICKET_IN_SUBJECT.findall(subject))
    return sorted(ids)


def _in_scope(files: list[str], scope: list[str]) -> list[str]:
    return [f for f in files if any(patterns_overlap(f, p) for p in scope)]


def _conflict(root: str, base: str, branch: str) -> tuple[str, list[str]]:
    # --write-tree only adds unreferenced objects; no ref, index or worktree moves.
    res = _git(root, "merge-tree", "--write-tree", "--name-only", "--no-messages",
               base, branch)
    if res.returncode == 0:
        return "merges_cleanly", []
    if res.returncode == 1:
        lines = res.stdout.splitlines()
        return "conflicts_with_default", [ln for ln in lines[1:] if ln.strip()]
    return "unknown", []


def unfinished_branches(repo_root: str, *, scope: list[str] | None = None,
                        tickets: list[int] | None = None,
                        limit: int = MAX_BRANCHES_REPORTED) -> list[BranchWork]:
    """Branches with patches not on the default branch, relevant to `scope` or
    naming one of `tickets`. Empty scope and no tickets means nothing is
    relevant, not everything: a project-wide dump is the noise that teaches
    workers to skip the section."""
    scope = [p for p in (scope or []) if p]
    wanted = set(tickets or [])
    if not repo_root or not (scope or wanted) or not Path(repo_root).exists():
        return []
    base = default_branch(repo_root)
    if not base:
        return []
    # Cheap filters first (one call, then one diff per unmerged branch); the
    # patch-equivalence and merge checks run only on branches that matter here.
    heads = _git(repo_root, "for-each-ref", f"--no-merged={base}",
                 "--format=%(refname:short)", "refs/heads")
    worktrees = _worktrees(repo_root)
    found: list[BranchWork] = []
    for branch in heads.stdout.split():
        changed = _git(repo_root, "diff", "--name-only", f"{base}...{branch}").stdout.split()
        in_scope = _in_scope(changed, scope) if scope else []
        subjects = _git(repo_root, "log", "--format=%s", "-n", "20",
                        f"{base}..{branch}").stdout.splitlines()
        ids = _tickets(branch, subjects)
        if not in_scope and not (wanted & set(ids)):
            continue
        cherry = _git(repo_root, "cherry", base, branch)
        unique = [ln.split()[1] for ln in cherry.stdout.splitlines() if ln.startswith("+ ")]
        if not unique:
            continue  # every patch already landed on the default branch (e.g. squash)
        path, session = worktrees.get(branch, ("", ""))
        conflict, conflict_files = _conflict(repo_root, base, branch)
        found.append(BranchWork(
            branch=branch, tickets=ids, unique_commits=len(unique),
            last_commit=_git(repo_root, "log", "-1", "--format=%cs", branch).stdout.strip(),
            files_in_scope=in_scope[:10], worktree=path, worktree_session=session,
            conflict=conflict, conflict_files=conflict_files[:10]))
    found.sort(key=lambda b: (b.last_commit, b.branch), reverse=True)
    return found[:limit]
