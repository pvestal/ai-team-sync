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
