"""#2761: a generated BASE-vs-CANDIDATE differential for the lock readers.

BASE is the reader rule deployed before #2761 (afc0fcd: routers/locks.check_locks
and routers/git_status.pre_commit_check), restated as an oracle: skip the lock when
both roots are set and differ as strings, otherwise fnmatch(path, pattern).

The one intended loss of coverage is a lock of ANOTHER repository: an anchored,
repo-relative lock, and a query located outside that lock's root. `other_repository`
decides that from locations alone. It never sees the pattern or the candidate's
answer, so it cannot excuse an ordinary false negative.

The inverse direction is pinned too: on canonically spelled input the candidate
equals an independent repository-relative oracle, and a non-canonical spelling
never covers more than its canonical spelling or the old rule did.
"""
from __future__ import annotations

import fnmatch
import itertools
import posixpath
from functools import lru_cache

import pytest

from ai_team_sync import scope_paths as sp

A, B, AB, NEST = "/srv/a", "/srv/b", "/srv/ab", "/srv/a/sub"

REL = ["x.py", "src/x.py", "src", "src/", "src/.", "src/sub/", "src/sub/.", "src/x/..",
       "src/sub/deep/x.py", "a/b/c/d/e/f.py", "./src/x.py", "src/./x.py", "src//x.py",
       "src/../x.py", "../x.py", "../b/src/x.py", "docs/a.md", "docs/", "pkg/mod/deep/nested/file.py"]
PATHS = (REL + [f"{r}/{p}" for r in (A, B, NEST) for p in REL]
         + [A, A + "/", AB + "/src/x.py", "/srv/a/../b/src/x.py", "//srv/a/src/"])
CALLER_ROOTS = ["", A, B, AB, NEST, "/srv//a/"]
PATTERNS = ["x.py", "src/x.py", "src", "src/", "src/.", "*", "**", "*.py", "src/*", "src/**",
            "./src/**", "src//*", "src/*/x.py", "src/**/x.py", "**/x.py", "src/sub/**", "*/src/**",
            "src/?", "docs/*.md", "docs/**", "pkg/*/deep/**", "/srv/a/src/**", "/srv/*/src/**"]
LOCK_ROOTS = ["", A, B, "/srv/a/", NEST]
CASES = list(itertools.product(PATHS, CALLER_ROOTS, PATTERNS, LOCK_ROOTS))


def base_covers(path, caller_root, pattern, lock_root):
    a, b = caller_root.rstrip("/"), lock_root.rstrip("/")
    if a and b and a != b:
        return False
    return fnmatch.fnmatch(path, pattern)


@lru_cache(maxsize=None)
def _query(path, caller_root):
    return sp.reader_query(path, caller_root)


@lru_cache(maxsize=None)
def _lock(pattern, lock_root):
    return sp.reader_lock(pattern, lock_root)


def candidate(path, caller_root, pattern, lock_root):
    return sp.reader_covers(_query(path, caller_root), _lock(pattern, lock_root))


def _abs(text):
    return "/" + posixpath.normpath(text).lstrip("/")


def _location(path, caller_root):
    """Where the query is, or None: rootless relative, or a relative path escaping its root."""
    if path.startswith("/"):
        return _abs(path)
    if not caller_root:
        return None
    rel = posixpath.normpath(path)
    if rel == ".." or rel.startswith("../"):
        return None
    return _abs(f"{caller_root}/{rel}")


def _inside(where, root):
    return where == root or where.startswith(root.rstrip("/") + "/")


def other_repository(path, caller_root, pattern, lock_root):
    if not lock_root or pattern.startswith("/"):
        return False
    where = _location(path, caller_root)
    return where is not None and not _inside(where, _abs(lock_root))


# ── coverage is never lost, except to another repository ─────────────────────

def test_the_corpus_is_bounded_and_nontrivial():
    assert 40_000 < len(CASES) < 80_000
    assert sum(base_covers(*c) for c in CASES) > 5_000


def test_base_coverage_is_lost_only_to_another_repository():
    lost = [c for c in CASES if base_covers(*c) and not candidate(*c) and not other_repository(*c)]
    assert not lost, lost[:20]


def test_another_repositorys_lock_never_covers():
    leaked = [c for c in CASES if other_repository(*c) and candidate(*c)]
    assert not leaked, leaked[:20]
    excused = [c for c in CASES if other_repository(*c) and base_covers(*c)]
    assert excused, "the corpus must exercise the cross-repository exception"


def test_an_excused_query_moved_into_the_locks_repository_is_covered():
    """The exception is about WHERE the query is. Base only ever covered another
    repository's lock through an absolute path (a rooted relative query was already
    skipped by its string compare), so move each excused absolute query to the same
    repo-relative place under the lock's root: it must be covered exactly when its
    repo-relative path matches the pattern."""
    moved, missed = 0, []
    for path, caller_root, pattern, lock_root in CASES:
        if not path.startswith("/") or not other_repository(path, caller_root, pattern, lock_root):
            continue
        if not base_covers(path, caller_root, pattern, lock_root):
            continue
        where = _location(path, caller_root)
        repo = next((r for r in (NEST, AB, A, B) if where.startswith(r + "/")), None)
        if repo is None:
            continue
        rel = where[len(repo) + 1:] + ("/" if path.endswith("/") else "")
        if not (_canonical(rel) and _canonical(pattern)) or not fnmatch.fnmatch(rel, pattern):
            continue
        moved += 1
        case = (f"{_abs(lock_root)}/{rel}", "", pattern, lock_root)
        if not candidate(*case):
            missed.append(case)
    assert moved > 0
    assert not missed, missed[:20]


@pytest.mark.parametrize("case,expected", [
    (("src/x.py", B, "src/**", A), True),
    (("src/x.py", A, "src/**", A), False),
    (("src/x.py", "/srv//a/", "src/**", "/srv/a/"), False),
    (("x.py", AB, "**", A), True),
    (("/srv/ab/x.py", "", "**", A), True),
    (("/srv/a/sub/x.py", "", "**", A), False),
    (("/srv/a/x.py", B, "**", A), False),
    (("../b/src/x.py", A, "**", A), False),
    (("src/x.py", "", "src/**", A), False),
    (("/srv/b/src/x.py", "", "src/**", ""), False),
    (("/srv/b/src/x.py", "", "/srv/*/src/**", A), False),
])
def test_other_repository_oracle(case, expected):
    assert other_repository(*case) is expected


# ── the inverse: no new coverage beyond what placement explains ──────────────

def _canonical(text):
    body = text[1:] if text.startswith("/") else text
    body = body[:-1] if body.endswith("/") else body
    return bool(body) and all(seg not in ("", ".", "..") for seg in body.split("/"))


def expected_canonical(path, caller_root, pattern, lock_root):
    """Independent oracle for canonically spelled path and pattern."""
    where = _location(path, caller_root)
    if where is not None and path.endswith("/"):
        where += "/"
    if pattern.startswith("/"):
        placed = fnmatch.fnmatch(where if where is not None else path, pattern)
    elif not lock_root:
        if where is None:
            placed = fnmatch.fnmatch(path, pattern)
        else:
            parts = where.lstrip("/").split("/")
            placed = any(fnmatch.fnmatch("/".join(parts[i:]), pattern) for i in range(len(parts)))
    elif where is None:
        placed = fnmatch.fnmatch(path, pattern)
    else:
        root = _abs(lock_root)
        if where == root:
            placed = False
        elif not where.startswith(root.rstrip("/") + "/"):
            return False
        else:
            placed = fnmatch.fnmatch(where[len(root.rstrip("/")) + 1:], pattern)
    return placed or base_covers(path, caller_root, pattern, lock_root)


def test_canonical_input_matches_the_independent_oracle():
    cases = [c for c in CASES if _canonical(c[0]) and _canonical(c[2])]
    assert len(cases) > 10_000
    wrong = [c for c in cases if candidate(*c) != expected_canonical(*c)]
    assert not wrong, wrong[:20]


def test_rootless_relative_canonical_queries_equal_the_old_rule():
    cases = [c for c in CASES if not c[0].startswith("/") and not c[1] and _canonical(c[0]) and _canonical(c[2])]
    assert cases
    assert [c for c in cases if candidate(*c) != base_covers(*c)] == []


def _spelled_canonically(text):
    lead = "/" if text.startswith("/") else ""
    segments = [s for s in text.split("/") if s not in ("", ".")]
    directory = "/" in text and text.rsplit("/", 1)[1] in ("", ".", "..")
    return lead + "/".join(segments) + ("/" if directory and segments else "")


def test_a_noncanonical_spelling_covers_no_more_than_its_canonical_spelling_or_the_old_rule():
    grown = []
    for path, caller_root, pattern, lock_root in CASES:
        if ".." in path.split("/") or (_canonical(path) and _canonical(pattern)):
            continue
        canon = (_spelled_canonically(path), caller_root, _spelled_canonically(pattern), lock_root)
        if not _canonical(canon[0]) or not _canonical(canon[2]):
            continue
        case = (path, caller_root, pattern, lock_root)
        if candidate(*case) and not (candidate(*canon) or base_covers(*case)):
            grown.append(case)
    assert not grown, grown[:20]


def test_a_pattern_nothing_is_named_after_covers_nothing():
    assert not [c for c in itertools.product(PATHS, CALLER_ROOTS, ["nomatch/**", "/srv/none/**"], LOCK_ROOTS)
                if candidate(*c)]


def test_the_suffix_pattern_equals_every_slash_boundary_suffix():
    tails = {_abs(p).lstrip("/") + ("/" if p.endswith("/") else "") for p in PATHS if p.startswith("/")}
    tails |= {f"srv/a/{p}" for p in REL}
    for tail, pattern in itertools.product(tails, [p for p in PATTERNS if not p.startswith("/")]):
        parts = tail.split("/")
        explicit = any(fnmatch.fnmatch("/".join(parts[i:]), pattern) for i in range(len(parts)))
        assert (fnmatch.fnmatch(tail, pattern) or fnmatch.fnmatch(tail, "*/" + pattern)) is explicit, (tail, pattern)
