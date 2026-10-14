# Commit, merge and shell-write guards (#3581 part B)

ATS locks are a claim. Each layer described here checks that claim at one
choke point. None of them covers every way a file can change. This document
states what each layer checks, what it misses, and how it can be bypassed.

On 2026-10-13 a `git merge` (9af31489) changed five files that another live
session had claimed. The only enforcement at the time was the Claude PreToolUse
edit guard, which never sees a merge, so nothing stopped it.

## The layers

| Layer | Runs when | Module |
|-------|-----------|--------|
| 1. Edit guard (pre-existing) | Claude Code calls Edit / Write / MultiEdit / NotebookEdit | `hooks/pre_tool_use_lockcheck.py` |
| 2. Bash write guard (new, best effort) | Claude Code calls the Bash tool | `hooks/pre_tool_use_bashguard.py` |
| 3. pre-commit (rewritten) | `git commit` | `hooks/pre_commit.py` |
| 4. pre-merge-commit (new) | `git merge` that creates a merge commit without conflicts | `hooks/pre_merge_commit.py` |

Layers 2 to 4 ask the server's lock reader, `POST /api/locks/check`, with
repo-relative paths, the **canonical repo root**, and the acting session id.
The canonical repo root is the main checkout, taken from
`git rev-parse --git-common-dir`. A worktree of a governed repository is
therefore checked against the same locks as its main checkout.

### The rule (layers 3 and 4)

These rules apply in a **governed** repository, meaning one listed under
`[governance].repositories` in `~/.config/ai-team-sync/operator.toml` or in the
legacy `ATS_COORDINATED_REPOS`:

| Situation | Result |
|-----------|--------|
| The committing ATS session is identified, and another live session holds a lock of **any** mode covering a staged path | refused |
| No ATS session can be identified (a human at a terminal) | refused only on another session's **exclusive** lock. Advisory holders are printed |
| ATS unreachable | **refused** (fail closed) |
| The operator config cannot be read | refused |

Outside governed repositories the earlier behavior stays. An exclusive lock
refuses, an advisory lock warns, and an unreachable ATS allows the commit.

### Identity: cooperative, not authenticated

An identity matters because it **removes that session's locks from the
verdict**. A process that borrows the lock holder's id would pass the guard.
The Bash guard and both git hooks share one resolver,
`commit_guard.resolve_identity`:

1. If a Claude session id is known, and that session's per-session pointer
   `~/.ats_session_<cid8>` exists, **the pointer wins**. The id comes from the
   hook payload, or else from `$CLAUDE_CODE_SESSION_ID` or the live-cid record.
   The Claude Bash tool's environment carries that variable, so a `git commit`
   run by an agent resolves to the agent's own pointer. A different
   `$ATS_SESSION_ID` is a **conflict**. The guard refuses and names both ids:
   the git hooks exit 1 and the Bash guard exits 2.
2. Otherwise `$ATS_SESSION_ID` is used, then a delegated child's capability
   file. Both are *self-asserted*. They are trusted only after ATS confirms two
   things:
   - **Ownership.** `/api/locks/check` is asked with that id and must not set
     `caller_identity_unresolved`. That is the server's #2741 check: the
     kernel's uid for the loopback socket against the session's `creator_uid`.
   - **Liveness.** `GET /api/sessions/<id>` must report `status: active`.
   - **Creator.** The same read's `creator_uid` must be this process's uid. A
     NULL `creator_uid` (a legacy row from before #2741) is unverified, and a
     failure in a governed repository.

   If an env identity fails any check, a governed repository **refuses**. An
   ungoverned repository judges the commit as unidentified (the
   exclusive-only rule).
3. The shared `~/.ats_session` file is never used, because it names whichever
   session on the machine wrote it last.

A session's own locks never refuse its own commit. An approved override clears
only the lock it was granted for.

**What this does not establish.** Every source above is a value that a process
of the same OS account can set. Identity is cooperative:

- A shell with no Claude pointer behind it, for example a human terminal, a
  script, or another agent without Claude's environment, can set
  `ATS_SESSION_ID` to **any active session its own account owns**, including the
  lock holder's. The guard then treats it as that session. The test
  `test_residual_same_account_env_identity_is_cooperative_not_authenticated`
  pins this behavior.
- A same-account process can write or overwrite a pointer file
  `~/.ats_session_<cid8>`, or set `CLAUDE_CODE_SESSION_ID` to another Claude
  session's id.
- The server's `cross_account` accepts a legacy session row whose `creator_uid`
  is NULL (created before #2741) for an ordinary account. The session API now
  returns `creator_uid`, and the guards treat NULL as unverified in a governed
  repository; in an ungoverned one such a row is still accepted.
- A **pointer-backed** identity that fails the same checks, for example
  because its session was completed or reaped, is not refused by the git and
  Bash guards. It is judged as unidentified. Only self-asserted identities are
  refused on failure.
- The edit guard (`pre_tool_use_lockcheck.py`) resolves identity the same way
  (`commit_guard.resolve_identity`): the pointer wins, a contradicting
  `$ATS_SESSION_ID` is refused (exit 2), and a self-asserted identity is
  validated with `identity_problem`. Failing validation refuses the edit in a
  governed repository and is judged unidentified elsewhere. It does not
  validate pointer-backed identities (unchanged).

**Paths checked.** pre-commit checks `git diff --cached --name-only --no-renames`,
where a rename counts as both its deleted and its added path. When `MERGE_HEAD`
exists, it also checks `HEAD...MERGE_HEAD`, which covers a conflicted merge
finished with `git commit`. pre-merge-commit checks the same set.

Measured on git 2.43: `MERGE_HEAD` has **not** been written yet when
pre-merge-commit runs, but the index already holds the merge result. The staged
set is therefore the full effect of the merge.

The three-dot form (`HEAD...MERGE_HEAD`) is used instead of two-dot
`HEAD MERGE_HEAD` on purpose. Two-dot would also list paths changed only on the
current branch, which the merge does not touch.

### Bypasses for layers 3 and 4

- `git commit --no-verify` and `git merge --no-verify`. git skips the hooks, and
  nothing is recorded.
- `ATS_COMMIT_GUARD_OVERRIDE=<reason>`. The commit proceeds and the override is
  printed. One JSON line is appended to
  `$ATS_COMMIT_GUARD_AUDIT_LOG`, or by default
  `${XDG_STATE_HOME:-~/.local/state}/ai-team-sync/commit-guard-audit.log`. The
  line holds the time, hook, reason, repo, cwd, user, session, paths, and the
  locks that would have refused, or the reason no check was possible. The log is
  a local file: anyone who can write it can edit it.

### Git operations that run neither hook

These were measured on git 2.43 with every hook installed. None of them runs
pre-commit or pre-merge-commit:

- a **fast-forward** merge or `git pull` (only post-merge runs)
- `git cherry-pick`, `git revert`, and the commits `git rebase` replays (only
  prepare-commit-msg and post-commit run)
- `git am` (applypatch-msg, pre-applypatch and post-applypatch run)
- `git checkout`, `git switch`, `git restore`, `git reset --hard`,
  `git stash pop` and `git apply`, which change the working tree without
  committing. A later `git commit` of those changes is checked.

Layer 2 sees these operations only when Claude runs them through the Bash tool.

## Layer 2: the Bash write guard

The guard parses the command statically. It never executes the command. It
extracts every path it can show the command writes:

| Construct | Paths |
|-----------|-------|
| `sed -i` (`-i.bak`, `-Ei`, `--in-place`), `perl -i` | the files operated on |
| `tee [-a]` | its files |
| `>`, `>>`, `>|`, `&>`, `&>>`, `<>`, `>&file` | the redirect target (fd duplication such as `2>&1` is ignored) |
| `cp`, `mv`, `install`, `rsync`, `ln` | the destination, or destination/basename when the destination is a directory; `-t DIR` / `--target-directory[=]DIR` (also clustered `-rt DIR` and attached `-tDIR`); `-T` (the destination is the file itself); `cp --parents` and `rsync -R` / `--relative`, which put the source path **as spelled** under the destination (rsync's `/./` marker is honored); `install -D SRC FILE`; files under a recursively copied directory; `mv` sources and `rsync --remove-source-files` sources. Forms it cannot place with certainty are reported as unresolved, not guessed: a source containing `..` with `--parents` / `-R`, `install -D` into an existing directory or with several sources, `-t` together with `-T`, `rsync --files-from`, remote rsync sources |
| `truncate`, `rm` (`-r` expands the directory), `unlink`, `shred` | the operands |
| `git apply`, `git am` | `git apply --numstat -z` on the patch file or on a `<` redirect |
| `git checkout [<rev>] -- <paths>`, `git restore` | pathspecs expanded with `git ls-files` / `git ls-tree` |
| `git checkout <branch>`, `git switch <branch>` | `git diff HEAD <branch>` |
| `git merge`, `git rebase`, `git pull` | `git diff HEAD...<ref>`. With no ref, `@{u}` is used |
| `git cherry-pick`, `git revert` | each commit's diff (ranges expanded with `rev-list`) |
| `git stash [push]` / `pop` / `apply` | the working changes, or `stash^1..stash` plus untracked files from `stash^3` |
| `git reset --hard/--merge/--keep [<rev>]` | the working changes plus `git diff HEAD <rev>` |
| `git rm`, `git mv`, `git clean` | pathspecs, and `git clean -n` output |
| `--abort` | the working changes (`git diff HEAD`) |

The guard also handles `cd`, `git -C`, env assignments, `sudo`, `env`,
`timeout`, `nice`, shell keywords (`if/then/do ...`), `$(...)` bodies, globs,
and heredoc bodies, which are data and are not treated as commands.

For each resolved target inside a git work tree:

| Repository | Another live session's lock covers it | ATS unreachable |
|------------|---------------------------------------|-----------------|
| governed, or any worktree of one | exit 2, holder named (any lock mode) | exit 2 (fail closed) |
| other | exclusive: exit 2. Advisory: warning added to the agent's context | allowed |

A target **named directly on the command line** inside a governed repository
must also be covered by the acting session's own live lock. This is the edit
guard's `claim_check`. Targets derived from git history, such as merge or
checkout results and recursive expansion, are checked only against other
sessions' locks. `ATS_LOCKCHECK_BLOCK=0` and `ATS_CLAIMCHECK=0` downgrade these
checks exactly as they do for the edit guard. Both are read from the Claude
process environment, not from the command.

If the parser itself fails with an internal error, the command is **allowed**.
A warning goes to stderr. A parser bug must not wedge every shell command.

### What the Bash guard cannot see

Each item below is allowed. Each one prints `ATS BASH GUARD: not checked -- ...`
on stderr, except where noted:

- interpreters: `python -c` / `python script.py`, `node`, `ruby`, `php`,
  `perl -e`, `awk`, `lua` and others
- `bash -c`, `sh -c`, `eval`, `source` / `.`, shell scripts run by path
- `xargs`, `find -exec` / `-delete`
- a target path containing a variable or command substitution (`"$f"`,
  `$(...)`), and relative targets after a `cd` it could not follow
- a patch piped on stdin (`cat p | git apply`)
- `git --git-dir` / `--work-tree`, `git rebase --continue` / `--skip`,
  `git cherry-pick --continue`, and restoring the original branch on
  `rebase --abort`
- commits a `git pull` fetches beyond the local remote-tracking ref
- **silently**, with no warning: every program it does not model, such as
  `make`, `npm`, `patch`, `dd`, `touch`, `chmod`, `tar -x`, `unzip`,
  `git submodule update`, compilers, formatters and test runners. Absence of a
  warning is not proof that nothing was written.

## Residual bypasses: what no layer here covers

- `git commit --no-verify`, `git merge --no-verify`, or deleting or editing the
  hook files
- writes by interpreters and arbitrary programs, from Claude's Bash tool or
  elsewhere (listed above)
- fast-forward merges, cherry-pick, revert, rebase and `git am` run **outside**
  Claude's Bash tool, for example by a human in a terminal, by Codex or another
  agent without these hooks, or by a script
- editors and IDEs outside Claude Code, and any agent that has not wired these
  PreToolUse hooks
- other OS users, and root. The hooks run as the committing user, and a
  repository's hook files can be changed by anyone who can write the repository
- a repository where `ats hooks install` was never run, or where
  `core.hooksPath` was later pointed elsewhere
- working-tree changes that are never committed. The commit hooks see only what
  is committed. Layers 1 and 2 see only what Claude's tools do
- ATS outages outside governed repositories (fail open by design), and an
  internal error in the Bash parser (fail open by design)
- the override variable and the override log. Both are trusted, local, and
  editable by the user they protect against
- session identity. It is cooperative, not authenticated (see "Identity"
  above). A same-account process without a Claude pointer can name the lock
  holder's active session in `ATS_SESSION_ID` and be treated as that session.
  A same-account process can also write pointer files
- the option tables the Bash guard uses for cp/mv/ln/install/rsync. An option
  that takes a value and is missing from them would be read as an operand


## Installing

The git hooks:

```
ats hooks install /path/to/repo            # pre-commit + pre-merge-commit
ats hooks install /path/to/repo --hook post-commit
./scripts/install-hooks.sh /path/to/repo   # same installer, legacy entry point
ats hooks uninstall /path/to/repo          # restores chained hooks
```

The installer writes to `git rev-parse --git-path hooks`. That is the common
git dir's `hooks/`, or `core.hooksPath` when set, so installing once covers
every worktree. An existing guard hook (pre-commit, pre-merge-commit,
prepare-commit-msg) is moved unchanged to `<hook>.ats-chained` and runs first.
If either the chained hook or the ATS hook fails, the commit fails. An existing
post hook (post-commit, post-checkout) is only appended to. If it is not a
sh/bash script, it is left untouched and reported. Re-running the installer
changes nothing.

Installing into a real repository is a deployment step and needs operator
approval. The tests use temporary repositories only.

The Bash guard is wired in `~/.claude/settings.json`. Shown here only; nothing
installs it. `settings.json` has ONE `PreToolUse` array: put this Bash guard and
the restart guard (`docs/restart-gate.md`) in the same `Bash` matcher, next to the
existing edit-guard matcher. Two separate `PreToolUse` keys are a duplicate JSON
key, and one guard silently disappears.

```json
{
  "hooks": {
    "PreToolUse": [
      { "matcher": "Edit|Write|MultiEdit|NotebookEdit",
        "hooks": [{ "type": "command", "timeout": 30, "onFailure": "block",
          "command": "<ats-venv>/bin/python -m ai_team_sync.hooks.pre_tool_use_lockcheck" }] },
      { "matcher": "Bash",
        "hooks": [
          { "type": "command", "timeout": 180, "onFailure": "block",
            "command": "<ats-venv>/bin/python -m ai_team_sync.hooks.pre_tool_use_bashguard" },
          { "type": "command", "timeout": 30, "onFailure": "block",
            "command": "<ats-venv>/bin/python -m ai_team_sync.hooks.pre_tool_use_restart_guard" }
        ] }
    ]
  }
}
```

Merge this into the existing `PreToolUse` array; keep every other matcher already
there (for example the edit guard and any curate hooks).

Claude Code runs all matching hooks in parallel and an exit 2 blocks the call. By
default a hook that crashes, cannot start or times out does NOT block: the call
goes ahead. `"onFailure": "block"` (Claude Code v2.1.295 or later) makes those
failures block, so a broken guard refuses instead of waving the call through. The
cost: if the ATS install itself is broken, every Bash call and edit is refused
until `settings.json` is fixed by hand, and a guard that runs past its timeout
refuses a valid command. Timeouts are sized to each guard's own budget: the edit
guard makes a few 2 s ATS calls and the restart guard at most a couple of 3 s
`systemctl show` lookups (30 s is ample for both); the Bash
guard runs git for analysis (10 s to find the repository, up to 20 s per ref
check, diff or `git apply --numstat`, 3 s per ATS call), so one `git merge` can
legitimately take ~55 s and a compound command with several git steps longer.
180 s covers several such steps; a command whose analysis needs more is refused
and must be split. The documentation does not state how an
exit 2 from one parallel hook combines with an exit 0 from another; the deployment
rehearsal checks it empirically.
