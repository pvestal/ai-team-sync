# Lock readers: comparison contract (#2761)

`POST /api/locks/check` and `POST /api/git/pre-commit-check` answer one question:
*which live locks cover this path?* They are READERS. They answer a namespace
question, so the comparison is purely lexical: no `realpath`, `resolve`, `stat`,
existence check or symlink resolution, and no other filesystem I/O, on the
request path. Whether a file currently exists does not change the answer.

Mutation grants (`routers/authority.py`) and the Claude edit hook have their own
comparisons and are not governed by this document. Session-creation and
`POST /api/locks` conflict checks (Gap 4) are pattern-vs-pattern; see
*Acquiring a lock* below.

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

> **Coverage is not the verdict (#2757, shipped `c7a6334`).** The sentence above
> describes COVERAGE, and coverage never depends on who asks: `/api/locks/check`
> still reports the caller's own lock, because "which live locks cover this path"
> has one answer for everyone.
>
> `pre-commit-check` also renders a VERDICT, and a verdict does depend on the
> caller: a session is never blocked by its OWN locks. It resolves the caller via
> `caller_session.resolve_caller_session` — explicit `session_id`,
> `X-ATS-Session-Id`, or `X-ATS-Agent` when unambiguous for the account — each
> validated with `cross_account` against the kernel's owner of the requesting
> socket. Foreign exclusive locks still block. A caller that cannot be resolved
> keeps the full conservative answer and is told so in `caller_identity_unresolved`.
> The task brief's "BLOCKERS NOW" uses the same resolver, so the two cannot drift.
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
| `hooks/pre_commit.py`, `hooks/pre_merge_commit.py` | staged paths + the canonical `repo_root` (main checkout, via `git rev-parse --git-common-dir`) + the committing `session_id` when one is identified (#3581) | yes. Rules and limits: [commit-guard.md](commit-guard.md). Installed only where `ats hooks install` was run |
| `hooks/pre_tool_use_bashguard.py` | resolved Bash write targets + canonical `repo_root` + `session_id` (#3581) | yes, for the targets it can resolve; see [commit-guard.md](commit-guard.md) |

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

## Acquiring a lock (#3581)

Session creation with `auto_lock`, `POST /api/locks` and MCP `extend_scope`
(which calls `POST /api/locks`) share one acquisition check
(`routers/sessions.py`: `refuse_unbacked_patterns`, `_check_scope_conflicts`,
`blocking_conflict`). Unlike the readers above, it consults the filesystem.

**Placement.** Before two patterns are compared, each is placed in a repository
by its OWN lock's `repo_root` (`repo_files.RepoContext.place`). A linked
worktree resolves through `git rev-parse --git-common-dir` to its main checkout,
so a worktree and its main checkout are one repository. A relative pattern
belongs to its session's repository (an unanchored session's to every
repository). An absolute pattern is relativized by the deepest checkout of that
repository containing it, else by wherever it points (the checkout of its
deepest existing directory). Patterns placed in different repositories never
overlap; an absolute pattern no checkout contains is compared lexically and
conservatively.

**Overlap.** Two placed patterns overlap when they can name a common file
(`scope_paths.patterns_overlap`). A file is covered by a pattern when either
enforcement reader says so: the edit hook's `scope_matches` (prefix for `/**`,
one level for `/*`, `fnmatch` otherwise) or `reader_covers` above. In a git
repository overlap is decided on its real files: `git ls-files` (tracked plus
untracked-not-ignored) over the main checkout and every linked worktree, where
a path through an in-repo symlink is the file it reaches (`lnk/a.py` is
`src/a.py` when `lnk -> src`; one level of directory links is expanded). Overlap
means a real file covered by both through any of its names, or a glob-free path
not yet created whose real location the other covers. Without a usable checkout
the test is algebraic and conservative: compatible literal prefixes are overlap.
Both are never narrower than the old two-way `fnmatch`, so `frontend/*/X.vue`
and `frontend/src/**` overlap exactly when a file such as `frontend/src/X.vue`
exists.

**Every anchored session**: an absolute pattern outside its `repo_root` is
refused 422 `pattern_outside_repo_root` (a lock the edit guard could never
honour), and when the operator config is unreadable every acquisition by an
anchored session is refused 503 `operator_config_unreadable` (fail closed;
unanchored legacy sessions are unaffected). A session holding live locks cannot
change its `repo_root` (409 `repo_root_change_with_live_locks`, nothing
applied); completing it in the same request is allowed.

**Serialized.** The conflict check, the insert and the commit are one step
(`routers/locks.py` `acquisition_guard`): one in-process `asyncio.Lock`, and the
acquiring transaction takes the database writer lock before it reads (SQLite
`BEGIN IMMEDIATE`, PostgreSQL `pg_advisory_xact_lock`), so concurrent
overlapping requests, from one server process or several, get one 201.

**Governed repositories** are those in `~/.config/ai-team-sync/operator.toml`
`[governance].repositories` (or `ATS_COORDINATED_REPOS`). There:

| Request | Answer |
|---------|--------|
| overlaps another live session's lock, advisory or exclusive | 409 `scope_conflict` naming the holder's session, agent and pattern |
| overlaps only the requesting session's own locks | allowed |
| the requester holds an approved override for that lock | allowed |
| a glob that matches no file | 422 `pattern_matches_no_file`, naming the nearest existing directory and same-named files |
| a glob-free path to a file that does not exist, parent directory exists | allowed (a new file) |
| a glob-free path whose parent directory does not exist | 422 `pattern_parent_missing` |
| a glob-free path naming a directory | 422 `pattern_names_a_directory` (use `dir/**`) |
| a pattern matching more files than `ATS_BROAD_CLAIM_LIMIT` (default 200; 0 or less disables) | 422 `pattern_too_broad` |

These rules apply to every lock mode and to new acquisitions only. A lock that
was already live when they shipped, or when a repository became governed, is not
revoked or re-judged: agents holding broad or unbacked locks keep them until
they complete, are reaped, or the lock expires. Lane restoration after a reap
goes through the same validation and overlap rule: a lane refused by the table
above is not re-granted (its `not_restored_reasons` entry is the refusal's
`error`), nor is one another session now holds in a governed repository.
Restoration is not inside `acquisition_guard`. Outside governed repositories the
old refusal rule stands: advisory locks share, and only an exclusive lock or an
exclusive request refuses an overlap.

**Before deploying.** Because live locks are not re-judged, a lock taken under
the old rules keeps acting under the new placement. `ats lock preflight-deploy
[--db PATH]` opens the server's SQLite database read-only and lists live locks
that placement makes `repo_wide` (an absolute `<worktree>/**` places as `**`) or
`broad` (over `ATS_BROAD_CLAIM_LIMIT`), that point into `other_repository` than
their session's anchor (never honoured by the edit guard, yet now conflicting),
that are `unplaceable`, or that match no file. It changes nothing and exits 1
when it finds any; clearing them is the holders' decision.

**Ownership report.** `GET /api/locks/ownership?repo_root=R&paths=a,b&session_id=S`
(MCP `lock_ownership`) lists, per path, every live session whose lock covers it
with its mode, and `conflict: true` when one of them is not `S`, after the same
placement and symlink resolution (`real` is the file a path reaches). It is the
final integration check before merging work that touched those paths.

What this does not do: a lock is a claim, not a filesystem permission. Only the
PreToolUse guard (Edit/Write/MultiEdit/NotebookEdit) consults it before a write;
a shell write, `git apply` or merge is checked, if at all, at commit time.
