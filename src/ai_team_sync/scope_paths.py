"""One canonical form for repo paths and scope patterns.

Every comparison between a path and a lock — claim coverage, exclusive-lock
conflicts, the Claude lockcheck hook — goes through here, so two spellings of
one file cannot be two different answers. Measured against the #2741 prototype:
with 'src/a.py' exclusively locked by another session, 'src//a.py',
'src/./a.py', 'src/[a].py' and 'src/?.py' were all authorized, because each
comparison used the raw string.

Two different jobs, deliberately separate:

  canonical_relpath / canonical_claim   for things that CONFER authority: an
      exact repo-relative file (or 'dir/**'). Anything ambiguous is refused
      rather than interpreted — glob characters (git pathspecs are globs, so
      'src/?.py' commits src/a.py), '..', absolute paths, backslashes, pathspec
      magic, control characters, '.git'.

  canonical_pattern / may_overlap       for things that can only DENY: another
      session's lock, written by any client in any form. These are normalized
      and compared conservatively; when overlap is uncertain the answer is yes.
"""

from __future__ import annotations

import fnmatch
import os
import posixpath
from dataclasses import dataclass, field
from typing import Optional

GLOB_META = frozenset("*?[]")


class UnsafePath(ValueError):
    """A path that cannot be given one unambiguous meaning."""


def canonical_relpath(path: object) -> str:
    if not isinstance(path, str) or not path:
        raise UnsafePath("path must be a non-empty string")
    if any(ord(c) < 32 or ord(c) == 127 for c in path):
        raise UnsafePath(f"{path!r} contains a control character")
    if "\\" in path:
        raise UnsafePath(f"{path!r} contains a backslash")
    if path.startswith("/"):
        raise UnsafePath(f"{path!r} is absolute; paths are repo-relative")
    if path.startswith(":"):
        raise UnsafePath(f"{path!r} looks like git pathspec magic")
    if GLOB_META & set(path):
        raise UnsafePath(f"{path!r} contains a glob character; name exact files")
    parts = [p for p in path.split("/") if p not in ("", ".")]
    if ".." in parts:
        raise UnsafePath(f"{path!r} contains '..'")
    if not parts:
        raise UnsafePath(f"{path!r} names the repository root, not a file")
    if ".git" in parts:
        raise UnsafePath(f"{path!r} is inside .git")
    if parts[0].startswith("-"):
        raise UnsafePath(f"{path!r} begins with '-'")
    return "/".join(parts)


def canonical_claim(pattern: object) -> str:
    """An authority-bearing claim: an exact file, or 'dir/**' for a subtree."""
    if isinstance(pattern, str) and pattern.endswith("/**"):
        return canonical_relpath(pattern[:-3]) + "/**"
    return canonical_relpath(pattern)


def claim_covers(claim: str, path: str) -> bool:
    """Exact semantics, both arguments already canonical. No globbing."""
    if claim.endswith("/**"):
        base = claim[:-3]
        return path == base or path.startswith(base + "/")
    return path == claim


def canonical_root(repo_root: object) -> str:
    """An absolute repo root, lexically normalized; '' if it is not one."""
    if not isinstance(repo_root, str) or not repo_root.strip().startswith("/"):
        return ""
    return "/" + posixpath.normpath(repo_root.strip()).lstrip("/")


def canonical_pattern(pattern: str, repo_root: str = "") -> str:
    """Lexical canonical form of an arbitrary scope/lock pattern.

    An absolute pattern under `repo_root` becomes relative; '//', '.', '..' and
    a trailing '/' collapse; glob characters are kept. An absolute pattern
    outside the repo stays absolute — it genuinely cannot describe a file here,
    and re-rooting it would invent a claim. A pattern naming the root itself
    returns '' (the caller decides what the whole repository means).
    """
    pat = (pattern or "").strip()
    if not pat:
        return ""
    if pat.startswith("/"):
        absolute = "/" + posixpath.normpath(pat).lstrip("/")
        root = canonical_root(repo_root)
        if root and root != "/" and (absolute == root or absolute.startswith(root + "/")):
            return absolute[len(root):].lstrip("/")
        return absolute
    normal = posixpath.normpath(pat)
    return "" if normal == "." else normal


def literal_prefix(parts: list[str]) -> tuple[list[str], bool]:
    """The leading components that contain no glob character, and whether any
    component after them does."""
    out: list[str] = []
    for part in parts:
        if GLOB_META & set(part):
            return out, True
        out.append(part)
    return out, False


def may_overlap(path: str, pattern: str) -> bool:
    """Could the exact `path` (a file or a directory) and `pattern` name a
    common file? Conservative: uncertainty is overlap.

    Both are relative or both absolute-without-leading-slash; `path` canonical.
    A directory path covers everything under it, so 'src' overlaps a lock on
    'src/a.py' — committing 'src' would commit that file.
    """
    if not pattern:
        return True
    if fnmatch.fnmatchcase(path, pattern):
        return True
    p_parts = path.split("/")
    l_parts = pattern.split("/")
    literal, has_glob = literal_prefix(l_parts)
    if not has_glob:
        return p_parts[:len(l_parts)] == l_parts or l_parts[:len(p_parts)] == p_parts
    n = min(len(literal), len(p_parts))
    return p_parts[:n] == literal[:n]


def resolved_relpath(repo_root: str, rel: str) -> str | None:
    """Where `rel` really lands after symlinks, relative to the real root.

    None when it lands outside the repository. When the repository is not
    readable by this process the lexical answer is returned unchanged, which is
    stated as a limitation rather than treated as proof.
    """
    real_root = os.path.realpath(repo_root)
    target = os.path.realpath(os.path.join(repo_root, rel))
    if target == real_root:
        return None
    if not target.startswith(real_root.rstrip("/") + "/"):
        return None
    return target[len(real_root.rstrip("/")) + 1:]


# ── lock readers (#2761) ─────────────────────────────────────────────────
# /api/locks/check and /api/git/pre-commit-check compare query paths with stored
# locks. Purely lexical: nothing below touches the filesystem, because a reader
# answers a namespace question on the server's event loop, and whether a file
# exists does not change who owns its name. The contract is docs/lock-readers.md.

@dataclass(frozen=True)
class ReaderQuery:
    raw_path: str
    raw_root: str
    absolute: str = ""                # placed in absolute space
    relative: str = ""                # a rootless relative path, normalised
    raw: bool = False                 # cannot be placed: the pre-#2761 comparison


@dataclass(frozen=True)
class ReaderLock:
    raw_pattern: str
    raw_root: str
    root: str = ""                    # lexical root of the owning session
    anchored: bool = False            # a repo-relative pattern with a root
    pattern: str = ""                 # anchored: absolute form; otherwise normalised
    relative: str = ""                # the pattern alone, normalised
    suffix_pattern: str = ""          # unanchored relative: matches any "/"-boundary suffix
    raw: bool = False


def _lexical_root(root: object) -> Optional[str]:
    """'' for no root, the normalised absolute root, or None for a non-empty root
    that is not absolute and so cannot be placed."""
    s = root.strip() if isinstance(root, str) else ""
    if not s:
        return ""
    if not s.startswith("/"):
        return None
    return "/" + posixpath.normpath(s).lstrip("/")


def _directory_form(text: str) -> bool:
    """`src/`, `src/.` and `src/x/..` name a directory; normalisation must keep
    the trailing '/' so `src/**` still covers them and `src` stays distinct."""
    return "/" in text and text.rsplit("/", 1)[1] in ("", ".", "..")


def _as_directory(form: str, directory: bool) -> str:
    return form + "/" if directory and not form.endswith("/") else form


def _lexical_pattern(text: str) -> str:
    """Drop empty and '.' segments, keeping directory form. Neither has glob
    meaning, so this is safe for a glob and equals posixpath.normpath for a
    literal without '..'."""
    lead = "/" if text.startswith("/") else ""
    segments = [s for s in text.split("/") if s not in ("", ".")]
    if not segments:
        return lead or "."
    return _as_directory(lead + "/".join(segments), _directory_form(text))


def _fnmatch_literal(text: str) -> str:
    """`text` as an fnmatch pattern that matches only itself."""
    return "".join(f"[{c}]" if c in "[]*?" else c for c in text)


def reader_query(path: object, caller_root: object) -> ReaderQuery:
    """Place one query path, once per request."""
    raw_path = path if isinstance(path, str) else ""
    raw_root = caller_root if isinstance(caller_root, str) else ""
    stripped = raw_path.strip()
    if not stripped or "\x00" in raw_path or "\x00" in raw_root:
        return ReaderQuery(raw_path, raw_root, raw=True)
    root = _lexical_root(raw_root)
    if root is None:
        return ReaderQuery(raw_path, raw_root, raw=True)
    directory = _directory_form(stripped)
    if stripped.startswith("/"):
        absolute = "/" + posixpath.normpath(stripped).lstrip("/")
    else:
        rel = posixpath.normpath(stripped)
        if rel in (".", "..") or rel.startswith("../"):
            return ReaderQuery(raw_path, raw_root, raw=True)
        if not root:
            return ReaderQuery(raw_path, raw_root, relative=_as_directory(rel, directory))
        absolute = f"{root.rstrip('/')}/{rel}"
    return ReaderQuery(raw_path, raw_root, absolute=_as_directory(absolute, directory))


def reader_lock(pattern: object, lock_root: object) -> ReaderLock:
    """Place one lock, once per request."""
    raw_pattern = pattern if isinstance(pattern, str) else ""
    raw_root = lock_root if isinstance(lock_root, str) else ""
    stripped = raw_pattern.strip()
    if (not stripped or "\x00" in raw_pattern or "\x00" in raw_root
            or ".." in stripped.split("/")):
        return ReaderLock(raw_pattern, raw_root, raw=True)
    root = _lexical_root(raw_root)
    if root is None:
        return ReaderLock(raw_pattern, raw_root, raw=True)
    rel = _lexical_pattern(stripped)
    if rel.startswith("/"):
        return ReaderLock(raw_pattern, raw_root, root=root, pattern=rel, relative=rel)
    if not root:
        return ReaderLock(raw_pattern, raw_root, pattern=rel, relative=rel, suffix_pattern="*/" + rel)
    base = _fnmatch_literal(root.rstrip("/"))
    placed = (base or "/") if rel == "." else f"{base}/{rel}"
    return ReaderLock(raw_pattern, raw_root, root=root, anchored=True, pattern=placed, relative=rel)


def _pre_2761_covers(query: ReaderQuery, lock: ReaderLock) -> bool:
    """The reader rule before #2761: skip when both roots are set and differ as
    strings, otherwise fnmatch the raw path against the raw pattern."""
    a = query.raw_root.rstrip("/")
    b = lock.raw_root.rstrip("/")
    if a and b and a != b:
        return False
    return fnmatch.fnmatch(query.raw_path, lock.raw_pattern)


def _placed_covers(query: ReaderQuery, lock: ReaderLock) -> bool:
    if lock.anchored and not query.absolute:
        # A rootless relative query has no repository identity: legacy match-
        # everywhere against the lock's own relative pattern (row 7).
        return fnmatch.fnmatch(query.relative, lock.relative)
    if lock.pattern.startswith("/"):
        return fnmatch.fnmatch(query.absolute or query.relative, lock.pattern)
    if query.absolute:
        # Any "/"-boundary suffix of the path: fnmatch's `*` crosses "/".
        tail = query.absolute.lstrip("/")
        return fnmatch.fnmatch(tail, lock.pattern) or fnmatch.fnmatch(tail, lock.suffix_pattern)
    return fnmatch.fnmatch(query.relative, lock.pattern)


def reader_covers(query: ReaderQuery, lock: ReaderLock) -> bool:
    """Whether `lock` covers `query` (docs/lock-readers.md). fnmatch only."""
    if query.raw or lock.raw:
        return _pre_2761_covers(query, lock)
    if lock.anchored and query.absolute:
        inside = lock.root.rstrip("/") + "/"
        if query.absolute != lock.root and not query.absolute.startswith(inside):
            return False  # another repository's lock: the one answer the old rule gets wrong
    # Placement adds coverage; it never removes what the old rule reported.
    return _placed_covers(query, lock) or _pre_2761_covers(query, lock)


# ── pattern-vs-pattern ownership (#3581) ──────────────────────────────────
# Whether two lock patterns can name a common file. A file "matches" a lock when
# EITHER enforcement reader says so: the edit hook's scope_matches (prefix '/**',
# one-level '/*', fnmatch otherwise) or reader_covers (/api/locks/check). Their
# union, so an overlap either surface would act on is seen here.

def lock_file_matcher(pattern: str, root: str = ""):
    """A predicate for repo-relative files `rel` under `root` that `pattern` covers."""
    from ai_team_sync.hooks.pre_tool_use_lockcheck import scope_matches

    lock = reader_lock(pattern, root)

    def matches(rel: str) -> bool:
        return (scope_matches(rel, pattern, root)
                or reader_covers(reader_query(rel, root), lock))
    return matches


def _same_path(rel: str) -> str:
    return rel


@dataclass(frozen=True)
class FileIndex:
    """Every name a repository's files answer to.

    `names` maps each name — a listed path, or a path reached through an in-repo
    symlink — to the REAL repo-relative file it reaches. `dir_aliases` holds
    (symlink, real directory) pairs so a file not created yet gets its alias
    names too, and `resolve` gives a not-yet-listed path's real location.
    """
    names: dict
    dir_aliases: tuple = ()
    resolve: object = _same_path
    aliases: dict = field(default_factory=dict)   # real -> its other names

    @classmethod
    def plain(cls, files) -> "FileIndex":
        return cls({f: f for f in files})

    @classmethod
    def build(cls, names: dict, dir_aliases=(), resolve=_same_path) -> "FileIndex":
        aliases: dict = {}
        for name, real in names.items():
            if name != real:
                aliases.setdefault(real, []).append(name)
        return cls(names, tuple(dir_aliases), resolve,
                   {real: tuple(other) for real, other in aliases.items()})

    def names_of(self, real: str) -> set:
        out = {real, *self.aliases.get(real, ())}
        for alias, target in self.dir_aliases:
            if real.startswith(target + "/"):
                out.add(alias + real[len(target):])
        return out


def as_index(files) -> "FileIndex | None":
    if files is None or isinstance(files, FileIndex):
        return files
    return FileIndex.plain(files)


def matching_files(pattern: str, files, root: str = "") -> frozenset[str]:
    """The real files `pattern` covers through any of their names."""
    index = as_index(files)
    matches = lock_file_matcher(pattern, root)
    return frozenset(real for name, real in index.names.items() if matches(name))


def covers_real(pattern: str, real: str, index: FileIndex, root: str = "", matcher=None) -> bool:
    matches = matcher or lock_file_matcher(pattern, root)
    return any(matches(name) for name in index.names_of(real))


def literal_target(pattern: str, root: str = "") -> str:
    """The one repo-relative path a glob-free pattern names; '' for a glob, the
    whole repository, or an absolute path outside `root`."""
    canon = canonical_pattern(pattern, root)
    if not canon or canon.startswith("/") or GLOB_META & set(canon):
        return ""
    return canon


def _algebraic_overlap(a: str, b: str) -> bool:
    """No file set to consult: compatible literal prefixes are overlap."""
    if not a or not b:
        return True
    if a.startswith("/") != b.startswith("/"):
        return True
    a_lit, a_glob = literal_prefix(a.split("/"))
    b_lit, b_glob = literal_prefix(b.split("/"))
    if not a_glob:
        return may_overlap(a, b)
    if not b_glob:
        return may_overlap(b, a)
    n = min(len(a_lit), len(b_lit))
    return a_lit[:n] == b_lit[:n]


def patterns_overlap(a: str, b: str, files=None, root: str = "",
                     a_matches: frozenset[str] | None = None) -> bool:
    """Can lock patterns `a` and `b` name a common file?

    Both are relative to the same repository `root`. With `files` (a FileIndex,
    or a plain set of paths): yes when some real file is covered by both through
    any of its names (a symlink alias is the same file), or when either is a
    literal path not listed yet (a file not created) whose real location the
    other covers. Without it: a conservative literal-prefix test where
    uncertainty is overlap. Neither form is ever narrower than the legacy
    two-way fnmatch. `a_matches` is `matching_files(a, files, root)` when the
    caller already has it.
    """
    if a == b or fnmatch.fnmatch(a, b) or fnmatch.fnmatch(b, a):
        return True
    index = as_index(files)
    if index is None:
        return _algebraic_overlap(canonical_pattern(a, root), canonical_pattern(b, root))
    if a_matches is None:
        a_matches = matching_files(a, index, root)
    b_matcher = lock_file_matcher(b, root)
    if any(covers_real(b, real, index, root, b_matcher) for real in a_matches):
        return True
    for literal, other in ((literal_target(a, root), b), (literal_target(b, root), a)):
        if literal and literal not in index.names:
            matcher = lock_file_matcher(other, root)
            if matcher(literal) or covers_real(other, index.resolve(literal), index, root,
                                               matcher):
                return True
    return False
