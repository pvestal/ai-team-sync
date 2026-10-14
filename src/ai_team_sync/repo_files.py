"""Real file sets behind lock patterns (#3581).

A lock pattern is only as good as the files it names: 'frontend/src/**/X.vue'
in a repo with no frontend/src protects nothing, and two patterns overlap when
some file matches both, which fnmatch-ing one pattern against the other cannot
tell. This module answers, for a session's repo_root, which repository it is
(a worktree resolves to its main checkout via `git rev-parse --git-common-dir`),
whether the operator governs it, and which files exist there — the union of
`git ls-files` over the main checkout and every linked worktree, since a session
anchored at the main checkout may be working on a file that so far exists only
on a worktree's branch. A path through an in-repo symlink is the file it
reaches, so 'lnk/a.py' and 'src/a.py' are one file when lnk -> src.

Every pattern is placed in a repository before it is compared: a relative
pattern belongs to its own lock's repo_root, an absolute one to wherever it
points. Two patterns placed in different repositories never overlap.

One RepoContext per request: git runs at most once per root per request. A git
failure reads as "no file set", which callers treat conservatively; an
unreadable operator config refuses anchored acquisitions (fail closed).
"""

from __future__ import annotations

import fnmatch
import logging
import os
import subprocess
from concurrent.futures import ThreadPoolExecutor

from ai_team_sync.operator_config import OperatorConfigError, governed_repositories
from ai_team_sync.scope_paths import (
    GLOB_META, FileIndex, _algebraic_overlap, canonical_pattern, literal_prefix,
    literal_target, matching_files, patterns_overlap)

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


def toplevel(directory: str) -> str:
    """The checkout `directory` is inside; '' when it is in none."""
    out = _git(["rev-parse", "--show-toplevel"], directory)
    return out.strip() if out else ""


def main_checkout(root: str) -> str:
    """The main checkout `root` belongs to; '' when `root` is not a checkout's top."""
    if not root or not os.path.isabs(root) or not os.path.isdir(root):
        return ""
    common = _git(["rev-parse", "--git-common-dir"], root)
    top = toplevel(root)
    if common is None or not top:
        return ""
    if os.path.realpath(top) != os.path.realpath(root):
        return ""  # a subdirectory: its patterns are not relative to the checkout
    common = os.path.realpath(os.path.join(top, common.strip()))
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


def _list_checkout(root: str) -> tuple[list[str], list[str]] | None:
    """(paths, symlinks) in one checkout: tracked plus untracked-not-ignored."""
    staged = _git(["ls-files", "-s", "-z"], root)
    other = _git(["ls-files", "-o", "--exclude-standard", "-z"], root)
    if staged is None or other is None:
        return None
    paths, links = [], []
    for record in staged.split("\0"):
        if not record:
            continue
        meta, _tab, path = record.partition("\t")
        paths.append(path)
        if meta.startswith("120000"):
            links.append(path)
    for path in other.split("\0"):
        if path:
            paths.append(path)
            if os.path.islink(os.path.join(root, path)):
                links.append(path)
    return paths, links


def _relative_inside(path: str, root: str) -> str | None:
    """`path` relative to `root` after resolving both; None when outside."""
    real, real_root = os.path.realpath(path), os.path.realpath(root)
    if real == real_root:
        return ""
    if real.startswith(real_root.rstrip("/") + "/"):
        return real[len(real_root.rstrip("/")) + 1:]
    return None


class RepoContext:
    """Per-request cache of repository identity, placement and file indexes."""

    def __init__(self) -> None:
        self._main: dict[str, str] = {}
        self._top: dict[str, str] = {}
        self._index: dict[str, FileIndex | None] = {}
        self._dirs: dict[str, frozenset[str]] = {}
        self._checkouts: dict[str, list[str]] = {}
        self._covered: dict[tuple[str, str], frozenset[str]] = {}
        self.config_error = ""
        try:
            self._governed = set(governed_repositories())
        except OperatorConfigError as exc:
            log.warning("operator config unreadable; anchored acquisitions refused: %s", exc)
            self.config_error = str(exc)
            self._governed = set()

    # ── identity ──────────────────────────────────────────────────────────

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

    def _toplevel(self, directory: str) -> str:
        if directory not in self._top:
            self._top[directory] = toplevel(directory)
        return self._top[directory]

    # ── placement ─────────────────────────────────────────────────────────

    def place(self, pattern: str, root: str) -> tuple[str, str] | None:
        """(repository identity, repo-relative pattern) for `pattern` held under
        `root`. Identity '' is an unanchored relative pattern, which applies in
        every repository. None: an absolute pattern no repository contains."""
        pat = (pattern or "").strip()
        root = (root or "").rstrip("/")
        if not pat.startswith("/"):
            return (self.identity(root) if root else "", canonical_pattern(pat, root))
        candidates = [root] if root else []
        main = self.main(root) if root else ""
        if main:
            candidates += self.checkouts(main)
        for checkout in sorted(set(candidates), key=len, reverse=True):
            rel = canonical_pattern(pat, checkout)
            if not rel.startswith("/"):
                return (self.identity(root), rel)
        return self._place_by_location(canonical_pattern(pat))

    def _place_by_location(self, absolute: str) -> tuple[str, str] | None:
        parts = absolute.lstrip("/").split("/")
        literal, _ = literal_prefix(parts)
        for k in range(len(literal), -1, -1):
            directory = "/" + "/".join(literal[:k])
            if os.path.isdir(directory):
                break
        top = self._toplevel(directory)
        if not top:
            return None
        rel_dir = _relative_inside(directory, top)
        if rel_dir is None:
            return None
        rel = "/".join(p for p in (rel_dir, "/".join(parts[k:])) if p)
        return (self.main(top) or top, rel)

    # ── files ─────────────────────────────────────────────────────────────

    def index(self, root: str) -> FileIndex | None:
        """Every file of the repository `root` belongs to, across worktrees,
        by every name; None when `root` is not a readable git checkout."""
        main = self.main(root)
        if not main:
            return None
        if main not in self._index:
            self._index[main] = self._build_index(main)
        return self._index[main]

    def _build_index(self, main: str) -> FileIndex | None:
        roots = self.checkouts(main)
        with ThreadPoolExecutor(max_workers=8) as pool:
            listings = list(pool.map(_list_checkout, roots))
        if listings[0] is None:
            return None
        names: dict[str, str] = {}
        links: list[tuple[str, str]] = []
        for root, listing in zip(roots, listings):
            if listing:
                names.update((p, p) for p in listing[0] if p not in names)
                links.extend((root, link) for link in listing[1])
        dir_aliases: list[tuple[str, str]] = []
        for root, link in links:
            target = os.path.join(root, link)
            rel = _relative_inside(target, root)
            if not rel or rel == link:
                continue  # outside the repository: the link is its own file
            if os.path.isdir(target):
                if (link, rel) not in dir_aliases:
                    dir_aliases.append((link, rel))
            else:
                names[link] = rel
        for alias, target in dir_aliases:
            for name, real in list(names.items()):
                if name == real and name.startswith(target + "/"):
                    names.setdefault(alias + name[len(target):], real)
        return FileIndex.build(names, dir_aliases, lambda rel: self._resolve(main, rel))

    def _resolve(self, main: str, rel: str) -> str:
        """Where a not-yet-listed `rel` would really be: its parent through symlinks."""
        parent, _, base = rel.rpartition("/")
        for checkout in self.checkouts(main):
            directory = os.path.join(checkout, parent)
            if os.path.isdir(directory):
                real_parent = _relative_inside(directory, checkout)
                if real_parent is not None:
                    return f"{real_parent}/{base}" if real_parent else base
        return rel

    def files(self, root: str) -> frozenset[str] | None:
        """Every name a file of the repository answers to."""
        index = self.index(root)
        return None if index is None else frozenset(index.names)

    def directories(self, root: str) -> frozenset[str]:
        """Every directory holding a listed name ('' = the repository root)."""
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
        parts, _ = literal_prefix(rel.split("/"))
        parts = list(parts)
        while parts and not self.is_directory(root, "/".join(parts)):
            parts.pop()
        return "/".join(parts)

    def same_basename(self, root: str, rel: str, limit: int = 3) -> list[str]:
        """Existing files sharing `rel`'s literal last component, for a hint."""
        name = rel.rsplit("/", 1)[-1]
        if not name or GLOB_META & set(name):
            return []
        hits = sorted(f for f in self.files(root) or ()
                      if f == name or f.endswith("/" + name))
        return hits[:limit]

    # ── decisions ─────────────────────────────────────────────────────────

    def covered(self, ident: str, rel: str) -> frozenset[str]:
        key = (ident, rel)
        if key not in self._covered:
            self._covered[key] = matching_files(rel, self.index(ident), ident)
        return self._covered[key]

    def overlap(self, pattern: str, root: str, other: str, other_root: str) -> bool:
        """Can `pattern` held under `root` and `other` held under `other_root`
        name a common file? Each is placed by its own root first."""
        a, b = self.place(pattern, root), self.place(other, other_root)
        if a is None or b is None:
            return self._absolute_overlap(a, pattern, b, other)
        (ia, ra), (ib, rb) = a, b
        if ia and ib and ia != ib:
            return False
        ident = ia or ib
        index = self.index(ident) if ident else None
        return patterns_overlap(ra, rb, index, ident,
                                a_matches=self.covered(ident, ra) if index else None)

    def _absolute_forms(self, placed, raw: str) -> list[str] | None:
        if placed is None:
            return [canonical_pattern(raw)]
        ident, rel = placed
        if not ident:
            return None  # an unanchored relative pattern is everywhere
        roots = self.checkouts(ident) if self.main(ident) else [ident]
        return [f"{r}/{rel}" if rel else r for r in roots]

    def _absolute_overlap(self, a, raw_a: str, b, raw_b: str) -> bool:
        forms_a, forms_b = self._absolute_forms(a, raw_a), self._absolute_forms(b, raw_b)
        if forms_a is None or forms_b is None:
            return True
        return any(x == y or fnmatch.fnmatch(x, y) or fnmatch.fnmatch(y, x)
                   or _algebraic_overlap(x, y) for x in forms_a for y in forms_b)

    def unbacked_reason(self, pattern: str, repo_root: str) -> tuple[int, str, str] | None:
        """(status, error, message) refusing a NEW lock `pattern` under
        `repo_root`, or None. Synchronous: it runs git."""
        root = (repo_root or "").rstrip("/")
        if not root:
            return None  # unanchored legacy acquisition
        if self.config_error:
            return (503, "operator_config_unreadable",
                    f"the operator config is unreadable, so ATS cannot tell whether "
                    f"{root} is governed; anchored acquisitions are refused until it is "
                    f"fixed: {self.config_error}")
        canon = canonical_pattern(pattern, root)
        if canon.startswith("/"):
            return (422, "pattern_outside_repo_root",
                    f"'{pattern}' is outside this session's repo_root {root}. Lock "
                    f"patterns are relative to it, so this lock could never be honoured; "
                    f"take it from a session anchored in that repository")
        governed = self.governed_root(root)
        index = self.index(root) if governed else None
        if index is None:
            return None
        literal = literal_target(pattern, root)
        matched = matching_files(pattern, index, root)
        if literal and not matched:
            if self.is_directory(root, literal):
                return (422, "pattern_names_a_directory",
                        f"'{pattern}' names a directory, which a lock does not cover; "
                        f"lock '{literal}/**' or the exact files")
            parent = literal.rsplit("/", 1)[0] if "/" in literal else ""
            if not self.is_directory(root, parent):
                nearest = self.nearest_directory(root, literal)
                return (422, "pattern_parent_missing",
                        f"'{pattern}' is a new file whose directory '{parent}' does not "
                        f"exist in {governed}; nearest existing directory: '{nearest or '.'}'")
            return None  # a new file in an existing directory
        if not matched:
            nearest = self.nearest_directory(root, canon)
            hint = self.same_basename(root, canon)
            return (422, "pattern_matches_no_file",
                    f"'{pattern}' matches no file in {governed}, so it would protect "
                    f"nothing; nearest existing directory: '{nearest or '.'}'"
                    + (f"; files with that name: {', '.join(hint)}" if hint else ""))
        limit = broad_claim_limit()
        if limit > 0 and len(matched) > limit:
            return (422, "pattern_too_broad",
                    f"'{pattern}' matches {len(matched)} files in {governed}, over the "
                    f"broad-claim limit of {limit} (ATS_BROAD_CLAIM_LIMIT); a claim that "
                    f"wide blocks every other session there. Narrow it to the files you "
                    f"will edit")
        return None
