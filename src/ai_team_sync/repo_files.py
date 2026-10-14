"""Real file sets behind lock patterns (#3581).

A lock pattern is only as good as the files it names: 'frontend/src/**/X.vue'
in a repo with no frontend/src protects nothing, and two patterns overlap when
some file matches both, which fnmatch-ing one pattern against the other cannot
tell. This module answers, for a session's repo_root, which repository it is
(a worktree resolves to its main checkout via `git rev-parse --git-common-dir`),
whether the operator governs it, and which files exist there — the union of
`git ls-files -co --exclude-standard` over the main checkout and every linked
worktree, since a session anchored at the main checkout may be working on a
file that so far exists only on a worktree's branch.

One RepoContext per request: git runs at most once per root per request. Every
git failure reads as "no file set", which callers treat conservatively.
"""

from __future__ import annotations

import logging
import os
import subprocess
from concurrent.futures import ThreadPoolExecutor

from ai_team_sync.operator_config import OperatorConfigError, governed_repositories

log = logging.getLogger(__name__)

GIT_TIMEOUT_SECONDS = 10
DEFAULT_BROAD_CLAIM_LIMIT = 200


def broad_claim_limit() -> int:
    """ATS_BROAD_CLAIM_LIMIT, default 200; a non-positive value disables it."""
    raw = (os.environ.get("ATS_BROAD_CLAIM_LIMIT") or "").strip()
    try:
        return int(raw) if raw else DEFAULT_BROAD_CLAIM_LIMIT
    except ValueError:
        return DEFAULT_BROAD_CLAIM_LIMIT


def _git(args: list[str], cwd: str) -> str | None:
    try:
        done = subprocess.run(["git", "-C", cwd, *args], capture_output=True,
                              timeout=GIT_TIMEOUT_SECONDS, check=False)
    except (OSError, subprocess.SubprocessError):
        return None
    if done.returncode != 0:
        return None
    return done.stdout.decode("utf-8", errors="surrogateescape")


def main_checkout(root: str) -> str:
    """The main checkout `root` belongs to; '' when `root` is not in a git checkout."""
    if not root or not os.path.isabs(root) or not os.path.isdir(root):
        return ""
    common = _git(["rev-parse", "--git-common-dir"], root)
    top = _git(["rev-parse", "--show-toplevel"], root)
    if common is None or top is None:
        return ""
    if os.path.realpath(top.strip()) != os.path.realpath(root):
        return ""  # a subdirectory: its patterns are not relative to the checkout
    common = os.path.realpath(os.path.join(top.strip(), common.strip()))
    if os.path.basename(common) == ".git":
        return os.path.dirname(common)
    return common  # bare repository: the common dir is its only identity


def checkouts(main: str) -> list[str]:
    """The main checkout and every linked worktree that exists on disk."""
    out = _git(["worktree", "list", "--porcelain"], main)
    roots = [main]
    for line in (out or "").splitlines():
        if line.startswith("worktree "):
            path = line[len("worktree "):]
            if path != main and os.path.isdir(path):
                roots.append(path)
    return roots


def _ls_files(root: str) -> list[str] | None:
    out = _git(["ls-files", "-co", "--exclude-standard", "-z"], root)
    return None if out is None else [p for p in out.split("\0") if p]


class RepoContext:
    """Per-request cache of repository identity and file sets."""

    def __init__(self) -> None:
        self._main: dict[str, str] = {}
        self._files: dict[str, frozenset[str] | None] = {}
        self._dirs: dict[str, frozenset[str]] = {}
        self._checkouts: dict[str, list[str]] = {}
        try:
            self._governed = set(governed_repositories())
        except OperatorConfigError as exc:
            log.warning("operator config unreadable; no repository is governed: %s", exc)
            self._governed = set()

    def main(self, root: str) -> str:
        key = (root or "").rstrip("/")
        if key not in self._main:
            self._main[key] = main_checkout(key)
        return self._main[key]

    def governed_root(self, root: str) -> str:
        """The governed main checkout `root` resolves to, or ''."""
        if not self._governed or not root:
            return ""
        main = self.main(root)
        real = os.path.realpath(main or root.rstrip("/") or "/")
        return real if real in self._governed else ""

    def identity(self, root: str) -> str:
        """One spelling per repository: its main checkout, else the raw root."""
        return self.main(root) or (root or "").rstrip("/")

    def checkouts(self, main: str) -> list[str]:
        if main not in self._checkouts:
            self._checkouts[main] = checkouts(main)
        return self._checkouts[main]

    def files(self, root: str) -> frozenset[str] | None:
        """Every file in the repository `root` belongs to, across worktrees;
        None when `root` is not a readable git checkout."""
        main = self.main(root)
        if not main:
            return None
        if main not in self._files:
            roots = self.checkouts(main)
            with ThreadPoolExecutor(max_workers=8) as pool:
                listings = list(pool.map(_ls_files, roots))
            if listings[0] is None:
                self._files[main] = None
            else:
                self._files[main] = frozenset(
                    p for listing in listings if listing for p in listing)
        return self._files[main]

    def directories(self, root: str) -> frozenset[str]:
        """Every directory holding a listed file ('' = the repository root)."""
        main = self.main(root)
        if main not in self._dirs:
            dirs = {""}
            for path in self.files(root) or ():
                parent = path
                while "/" in parent:
                    parent = parent.rsplit("/", 1)[0]
                    if parent in dirs:
                        break
                    dirs.add(parent)
            self._dirs[main] = frozenset(dirs)
        return self._dirs[main]

    def is_directory(self, root: str, rel: str) -> bool:
        if rel in self.directories(root):
            return True
        main = self.main(root)
        return bool(main) and any(os.path.isdir(os.path.join(c, rel))
                                  for c in self.checkouts(main))

    def nearest_directory(self, root: str, rel: str) -> str:
        """The deepest existing directory along `rel`'s literal prefix ('' = root)."""
        parts = []
        for part in rel.split("/"):
            if set("*?[]") & set(part):
                break
            parts.append(part)
        while parts and not self.is_directory(root, "/".join(parts)):
            parts.pop()
        return "/".join(parts)

    def same_basename(self, root: str, rel: str, limit: int = 3) -> list[str]:
        """Existing files sharing `rel`'s literal last component, for a hint."""
        name = rel.rsplit("/", 1)[-1]
        if not name or set("*?[]") & set(name):
            return []
        hits = sorted(f for f in self.files(root) or ()
                      if f == name or f.endswith("/" + name))
        return hits[:limit]
