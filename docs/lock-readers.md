# Lock readers: comparison contract (#2761)

`POST /api/locks/check` and `POST /api/git/pre-commit-check` answer one question:
*which live locks cover this path?* They are READERS. They answer a namespace
question, so the comparison is purely lexical: no `realpath`, `resolve`, `stat`,
existence check or symlink resolution, and no other filesystem I/O, on the
request path. Whether a file currently exists does not change the answer.

Mutation grants (`routers/authority.py`) and the Claude edit hook have their own
comparisons and are not governed by this document. Session-creation and
`POST /api/locks` conflict checks (Gap 4) are pattern-vs-pattern and unchanged.

## Terms

- **Query**: a `path`, plus the caller's optional `repo_root`.
- **Lock**: a `pattern`, plus the owning session's stored `repo_root`
  (empty = *unanchored legacy lock*).
- **Lexical root**: a non-empty `repo_root` that starts with `/`, normalised with
  `posixpath.normpath` (`/srv//a/` is `/srv/a`). A non-empty root that does not
  start with `/` is **not a root**; see *raw*.
- **Absolute form of a query**: an absolute `path`, normalised; or a relative
  `path` normalised and joined under the caller's lexical root. A relative path
  whose normalisation climbs out of the root (`..` first) has no absolute form.
- **Directory form**: a path or pattern whose last segment is empty, `.` or `..`
  (`src/`, `src/.`, `src/x/..`) names a directory. Normalisation keeps a trailing
  `/` on it, so `src/**` and `src/*` still cover `src/`, while `src` (no slash)
  stays a different name that `src/**` does not cover.
- **Absolute form of an anchored lock**: its lexical root joined with its
  pattern. The root is escaped for `fnmatch` (`[`, `*`, `?`), so a root is always
  compared literally. The pattern is normalised by dropping empty and `.`
  segments only (`./src//**` is `src/**`); neither has glob meaning, so glob
  characters are otherwise kept verbatim.
- **raw**: a query or lock that cannot be placed lexically (NUL anywhere in path,
  pattern or root; a non-absolute non-empty root; an empty path or pattern; a
  pattern with a `..` segment; a relative query that escapes its root). A raw
  comparison is exactly the pre-#2761 rule: skip the lock when both roots are
  non-empty and differ as strings, otherwise `fnmatch(path, pattern)`. It never
  raises.
- **Another repository's lock**: an anchored lock with a repo-relative pattern,
  and a query whose absolute form is neither its root nor under it (compared as
  strings on a `/` boundary).

## Never less than the old rule

Placement only ADDS coverage. A placeable query and lock are covered when the
placed comparison below covers them OR the pre-#2761 rule does, except for
another repository's lock, which never covers. That exception is the one answer
the pre-#2761 rule got wrong, and it is decided from locations alone.
`tests/test_lock_readers_differential.py` checks this against a generated corpus.

Matching is `fnmatch.fnmatch` throughout, so existing glob semantics are
unchanged: `*` and `?` may cross `/`, `[...]` is a character class, `**` is two
`*`.

## Truth table

`A` = `/srv/a`, `B` = `/srv/b`, `AB` = `/srv/ab`. Lock `A:src/**` = pattern
`src/**` held by a session anchored at `A`.

| # | Lock | Query path | Caller root | Comparison | Covered |
|---|------|-----------|-------------|------------|---------|
| 1 | `A:src/**` | `/srv/a/src/x.py` | `A` or none | `fnmatch("/srv/a/src/x.py", "/srv/a/src/**")` | **yes** |
| 2 | `A:src/**` | `src/x.py` | `A` | `fnmatch("/srv/a/src/x.py", "/srv/a/src/**")` | **yes** |
| 3 | `A:src/**` | `/srv/b/src/x.py` | any | absolute forms differ | no |
| 3b | `A:src/**` | `src/x.py` | `B` | `fnmatch("/srv/b/src/x.py", "/srv/a/src/**")` | no |
| 4 | `A:src/**` | `src/x.py` | `A` vs `B` | the repository is part of the namespace | `A` yes, `B` no |
| 5 | unanchored `src/**` | `src/x.py` | `B` | legacy: the pattern means that path in every repository | **yes** |
| 5b | unanchored `src/**` | `/srv/b/src/x.py` | any or none | `fnmatch` against every `/`-boundary suffix of the absolute form | **yes** |
| 6 | unanchored `src/**` | `src/x.py` | none | `fnmatch("src/x.py", "src/**")` | **yes** |
| 7 | `A:src/**` | `src/x.py` | none | no repository identity: legacy `fnmatch("src/x.py", "src/**")` | **yes** (conservative) |
| 8 | `A:src/**` | `/srv/a/src/x.py` | none | an absolute path carries its own location | **yes** |
| 8b | `A:src/**` | `/srv/a/src/x.py` | `B` | the caller root only places RELATIVE paths | **yes** |
| 9a | `A:src/*.py` | `src/readme.md` | `A` | `fnmatch("/srv/a/src/readme.md", "/srv/a/src/*.py")` | no |
| 9b | `A:*.py` | `docs/a.md` | `A` | no | no |
| 9c | `A:**/*.md` | `src/x.py` | `A` | no | no |
| 9d | `A:src/[ab].py` | `src/a.py` / `src/c.py` | `A` | character class | yes / no |
| 9e | `A:src/*` | `src/sub/deep.txt` | `A` | `*` crosses `/`, as before | yes |
| 10a | any | path containing NUL | any | raw | pre-#2761 answer, never HTTP 500 |
| 10b | pattern containing NUL | any | any | raw | pre-#2761 answer, never HTTP 500 |
| 10c | stored root `ra` (not absolute) | any | any | raw | pre-#2761 answer |
| 11 | `A:**` | `/srv/ab/x.py` | none | `fnmatch("/srv/ab/x.py", "/srv/a/**")` | no (no prefix collision) |
| 12 | `B:src/**` | `../b/src/x.py` | `A` | escapes `A`: raw, never re-rooted into `B` | no |
| 12b | `B:src/**` | `/srv/a/../b/src/x.py` | any | absolute, normalised to `/srv/b/src/x.py` | yes |
| 13 | `A:src/**` | `src/x.py` | `/srv//a/` | lexical root `/srv/a` | yes |
| 14 | `A:./src//**` | `./src/x.py` | `A` or none | pattern and path both normalised to `src/...` | yes |
| 15 | `A:src/**` | `src/`, `src/.`, `src/x/..` | `A` or none | directory form `src/`: `fnmatch("/srv/a/src/", "/srv/a/src/**")` | yes |
| 15b | `A:src/**` | `src/` | `B` | another repository's lock | no |
| 15c | unanchored `src/**` | `src/` | `B` | legacy | yes |
| 16 | `A:src/**` | `src` | `A` | `src` is not `src/` | no |
| 16b | `A:src` | `src/` | `A` | `src/` is not the file `src` | no |
| 17 | `A:src/` | `/srv/a/src/` | none | directory-form pattern | yes |

## Several locks cover one path

`/api/locks/check` returns one row per path. If any covering lock is exclusive,
that lock is reported; otherwise the first covering lock is. The mode of each
lock is never changed. `pre-commit-check` lists every covering lock and blocks on
any exclusive one, as before.

> **Known defect (#2757) — this is behaviour, not contract.** "Any exclusive
> one" includes the CALLER'S OWN exclusive locks. The endpoint takes
> `staged_files` and `repo_root` and no session identity, so a session using
> exclusive locks correctly is always told its own commit is blocked. Reporting
> the lock is right — this file's contract is coverage, and coverage does not
> depend on who asks. Rendering it as a BLOCKING VERDICT without knowing the
> caller is the bug. Do not treat the sentence above as the intended rule.
> See Gap 5 in `docs/product-gaps-reaper-and-scope.md`.

## Callers and repository identity

A reader can only be as repository-correct as the identity its caller supplies.
Repository identity is never inferred from a working directory, filesystem
probing or path coincidence.

| Caller | Sends | Cross-repo correct? |
|--------|-------|--------------------|
| MCP `check_locks` | `repo_root` when the caller passes it | yes for absolute paths, and for relative paths when `repo_root` is given |
| MCP `pre_commit_check` | `repo_root` when the caller passes it | same |
| MCP `whos_editing` | `repo_root` when the caller passes it | same |
| CLI `ats lock check` | paths only | absolute paths yes; relative paths legacy (row 7) |
| `hooks/pre_commit.py` | relative staged paths only | legacy (row 7); installed in no repository as of 2026-09-15 |

A rootless relative query stays conservative: an anchored lock from any
repository with a matching relative pattern is reported. That is a false
positive, never a false negative, and it is the documented limit for callers
that do not have repository identity. `ats lock check src/` sends `src/`
unchanged and stays blocked by an exclusive `src/**`.

## Cost

Each query path is canonicalised once and each lock once per request; the
matching loop is O(paths x locks) `fnmatch` calls, at most four per pair, with
compiled patterns cached by `fnmatch`. An unanchored lock matches every
`/`-boundary suffix through one extra pattern, `*/` + pattern, rather than one
call per suffix.
