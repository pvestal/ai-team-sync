# P0 Stage 1 GO/NO-GO packet: ATS deploy candidate (#3580 + #3581)

Stage 1 deploys the ATS candidate only. It changes no anime-studio code and
restarts no anime-studio unit. This packet was rehearsed on 2026-10-14 between
04:04 and 04:20 UTC, in a non-production copy under the session scratchpad. That
copy used a scratch pipx home, alternate ports 18400-18402, a backup-API copy of
ats.db, a scratch operator.toml, a `git clone --shared /opt/anime-studio`, and
`systemd-run --user` units named `ats-stage1-e53b106b-*`.

All of that was removed afterwards. Proof, at 04:20 UTC:

- 0 such units, 0 listeners on ports 18400-18409, and 0 processes left.
- Production is unchanged:
  - operator.toml and settings.json have the same sha256 as the baseline;
  - the venv's source-tree hash matches the baseline;
  - ats-server is still PID 4754 with NRestarts=0, and `/api/version` still
    reports d56f144 dirty;
  - the five governed units have the same MainPID and ActiveEnterTimestamp;
  - the `/opt` git hooks are unchanged.

One rehearsal command was refused by the session's permission classifier:
`ats safe-restart` against the production unit names, which the rehearsal
server would only have refused. That case was evaluated in-process instead (R5).

## 1. Candidate and verdicts

- **SHA: `f5cb28b`** on `feat/p0-ats-combined`.
  - `main` (d6038df) is an ancestor, so a fast-forward is possible.
  - Diff from main: 41 files.
  - Rehearsed at 12ad48a, 8626571 and f5cb28b. Under `src/`, these three differ
    only in `mcp/server.py` (delete_lock). The server, hook and loader evidence
    is therefore the same across all three; the final run is at f5cb28b.

| Commit | Content | Review |
|---|---|---|
| 5fc673b | #3581 integrated | Codex r3 PASS-WITH-CITATION (065d4a06) |
| 3e4eace | #3580 restart gate | Codex r3 PASS-WITH-CITATION (35c5e82e) |
| 15f13ba | `--no-ff` merge of the two | Codex integration r2 PASS-WITH-CITATION (58badc5b), together with bb41200 |
| bb41200 | Docs: one combined PreToolUse array | Codex integration r2 PASS-WITH-CITATION (58badc5b), together with 15f13ba |
| 12ad48a | Docs: onFailure | Codex BLOCK (cfd0983d): 30 s too short for the Bash guard; resolved in 8626571 |
| 8626571 | Hook timeouts 30/180/30 s; MCP delete_lock names its own session | Codex BLOCK (e451d7a0): delete_lock skipped deny_mutation; resolved in f5cb28b |
| f5cb28b | delete_lock names the caller only when the mutation rule accepts it | **Codex PASS-WITH-CITATION (e80b5c72, decision 753b99aa)** |

Owner-verified full suite at f5cb28b: 1625 passed, 42 skipped (1
workstation-config test deselected). This rehearsal did not re-run the suite.

## 2. What Stage 1 changes in production

1. **pipx venv `~/.local/share/pipx/venvs/ai-team-sync`.**
   - The code becomes f5cb28b.
   - The change is live **immediately** for every reader of that venv:
     - the already-wired edit guard (lockcheck, with new identity rules);
     - ats_context, autostart and heartbeat;
     - Codex hooks;
     - the existing post-commit hooks (post_commit itself is unchanged);
     - any newly spawned ats-mcp.
2. **ats-server restart (user unit).** This brings in:
   - placement refusals (409/422);
   - `/api/locks/ownership`;
   - restart leases and their sweeper.

   The schema change is **one new table, `restart_leases`**. No column changes.
3. **`~/.claude/settings.json`.** The PreToolUse array in section 4.
4. **`.git/hooks` in `/opt/anime-studio` and `/opt/tower-echo-brain`.**
   - Both get pre-commit and pre-merge-commit, each with an `-ats` runner.
   - tower-echo-brain's existing pre-commit becomes `pre-commit.ats-chained` and
     runs first.
   - post-commit is untouched.
5. **operator.toml: no change** (recommended; see section 5).
6. **Not touched:**
   - `~/.codex`. **Do not run scripts/deploy.sh**: it rewrites settings.json and
     `~/.codex`, and restarts ats-server.
   - All system units and anime-studio.

## 3. Preconditions (each is a GO gate)

| # | Precondition | How to verify |
|---|---|---|
| P1 | Codex verdict recorded for 12ad48a..f5cb28b, and the owner suite is green at f5cb28b | ticket notes |
| P2 | Lock hazards cleared. **Acceptable set: `matches_no_file` only.** These lock no real file, so they 409 nothing. | `ats lock preflight-deploy`, run with the new venv against the live DB `mode=ro`: exit 0, or only `[matches_no_file]` lines |
| P3 | No session mid-edit; ATS broadcast sent; Codex idle | team_status / whos_editing; `pgrep codex` |
| P4 | Backups taken (S1) | `ls` and sha256 of each backup |
| P5 | Every running Claude is v2.1.295 or later (`onFailure`) | `readlink /proc/<pid>/exe` of each `claude`. Today: 4 at 2.1.296; installed 2.1.299 |
| P6 | Build from a clean f5cb28b tree with the stamp written | stamp shows dirty=False |

**P2 is the blocker today.** The live DB at 04:18 UTC showed 126 locks and 6
hazards.

- **repo_wide.** Two locks held by 02428c7d (298b649f), both advisory `**` on
  anime-studio worktrees. These anchor to the whole of /opt/anime-studio (3747
  files). They expire at 09:05:27 and 11:31:58 UTC unless renewed.
- **broad.** 27fbdab4 (41e64e59) holds `frontend/**` (338 files) and
  `tests/**` (1404 files), advisory, expiring at 07:40:31 UTC.
- **matches_no_file.** 27fbdab4 holds two exclusive `frontend/src/**` locks.
  They protect nothing.

What deploying with these hazards present would do (proven on the copy, R3):

- Every new claim in /opt/anime-studio returned 409 naming 02428c7d.
- That included `packages/core/config.py`, `README.md` and
  `frontend/package.json`.
- An agent can still edit files it already holds a lock on.
- A new claim needs a new lock, so it fails. The edit guard's claim check then
  refuses the edit.

How the hazards clear:

| Path | Status today |
|---|---|
| The owner calls `complete_session` | works today |
| The owner deletes its own live lock with `?actor_session_id=<own id>` | The route on **production main is identical** to the candidate's, so this works today through REST. Through MCP it works **only from a f5cb28b ats-mcp**: an old MCP's `delete_lock` gets 403 `lock_not_yours` (R8). On the copy, all 6 hazard locks gave 403 without an actor and 204 with the owner's id; preflight then exited 0 and the three claims gave 201. |
| TTL expiry | advisory locks only |
| Reaper | the owner is silent for 20 min |

## 4. Step sequence (each step needs operator GO)

**S1 Backups.**

```
ts=$(date +%Y%m%d-%H%M%S)
cp -a ~/.local/share/pipx/venvs/ai-team-sync ~/.local/share/pipx/venvs/ai-team-sync.bak-$ts
sqlite3 ~/.local/share/ai-team-sync/ats.db ".backup ~/.local/share/ai-team-sync/ats.db.bak-$ts"
cp -a ~/.config/ai-team-sync/operator.toml ~/.config/ai-team-sync/operator.toml.bak-$ts
cp -a ~/.claude/settings.json ~/.claude/settings.json.bak-$ts
cp -a /opt/anime-studio/.git/hooks /opt/anime-studio/.git/hooks.bak-$ts
cp -a /opt/tower-echo-brain/.git/hooks /opt/tower-echo-brain/.git/hooks.bak-$ts
```

- Verify: each backup exists and has the same sha256 as its source.
- Back up the hooks directories. **Do not rely on `ats hooks uninstall`**: it
  iterates every module, so it also removes the *pre-existing* post-commit ATS
  call line and `post-commit-ats` (R4), which would silently stop commit
  recording.

**S2 Install.**

- Get a clean f5cb28b tree, either by ff `main` to f5cb28b in
  `/home/patrick/code/ai-team-sync` (D2) or from a clean clone.
- Write `src/ai_team_sync/_build_stamp.py` with the stamp lines from deploy.sh.
- Run `pipx install --force '<tree>[console]'`.
- Verify: the venv's
  `python -c "from ai_team_sync import build_info as b; print(b.identity('x'))"`
  prints commit f5cb28b, dirty False. `operator_config._TOP_LEVEL_KEYS` is
  `{governance, restart_gate}`.
- Notes:
  - pipx 1.4.3 `--force` is an **in-place** `pip --force-reinstall`
    (pipx install.py:69-71).
  - The rehearsed old-to-new upgrade took 8.5 s. A poller ran the real edit
    guard every ~0.2 s throughout (123 samples over 2 installs) and saw 0 import
    failures. The fail-open window is too short to observe, but it is not zero.
- Rollback: `rm -rf` the venv, then `mv` the backup back. This restores the
  exact bytes and the d56f144 stamp. The `cp -a` backup was proven runnable (R7).

**S3 Restart and check.**

- Run `systemctl --user restart ats-server` right after S2, so that no
  mixed-version server process stays up.
- Verify:
  - `/api/version` shows f5cb28b;
  - the journal shows startup complete and no Traceback;
  - `sqlite3 'file:...ats.db?mode=ro' '.schema restart_leases'`;
  - existing table row counts unchanged (R2: identical, only `restart_leases`
    added);
  - preflight is re-run.
- Gap: during the restart (~1 s), the **edit guard fails OPEN**. An unreachable
  ATS gives exit 0 (R4). The Bash guard fails closed.
- Rollback: the S2 rollback, then restart. The old code runs on the migrated DB
  (R7):
  - version d6038df;
  - sessions, locks and restarts readable;
  - claims created;
  - `restart_leases` ignored.

  Leave the table in place, and restore ats.db only on a separate ruling.

**S4 Claude settings.**

- Replace only `hooks.PreToolUse` in `~/.claude/settings.json` with the array
  below. It is computed read-only from the current file:
  - curate-gate is kept;
  - lockcheck gains `timeout`/`onFailure`;
  - a separate Bash matcher is added.

  The rehearsal proved that separate matchers both run, and that either one
  exiting 2 blocks (R6).

```json
"PreToolUse": [
  { "matcher": "Bash|mcp__.*", "hooks": [
      { "type": "command", "command": "/home/patrick/.claude/hooks/curate-gate.sh", "timeout": 5 } ] },
  { "matcher": "Edit|Write|MultiEdit|NotebookEdit", "hooks": [
      { "type": "command", "timeout": 30, "onFailure": "block",
        "command": "/home/patrick/.local/share/pipx/venvs/ai-team-sync/bin/python -m ai_team_sync.hooks.pre_tool_use_lockcheck" } ] },
  { "matcher": "Bash", "hooks": [
      { "type": "command", "timeout": 180, "onFailure": "block",
        "command": "/home/patrick/.local/share/pipx/venvs/ai-team-sync/bin/python -m ai_team_sync.hooks.pre_tool_use_bashguard" },
      { "type": "command", "timeout": 30, "onFailure": "block",
        "command": "/home/patrick/.local/share/pipx/venvs/ai-team-sync/bin/python -m ai_team_sync.hooks.pre_tool_use_restart_guard" } ] }
]
```

- Verify:
  - `python3 -m json.tool settings.json`;
  - feed the restart guard a payload, `sudo systemctl restart anime-studio`,
    and expect exit 2. Never run the command itself.
  - In a **new** Claude session, a refused `sed -i` on another session's file
    in a governed repo is blocked.

  Whether running sessions reload hooks without a restart was **not verified**.
- Cost: the guards add 0.03-0.11 s per Bash call when the command writes
  nothing.
- Rollback: restore the settings.json backup **from a terminal**. With
  `onFailure: block`, a broken ATS install refuses every edit and every Bash
  call until this is done (D6).

**S5 Git hooks.**

```
ats hooks install /opt/anime-studio --python <venv python>
ats hooks install /opt/tower-echo-brain --python <venv python>
```

- Expected output, derived from the installer code and from the rehearsal
  layout (a clone with a stand-in pre-commit plus the production post-commit
  pair):
  - anime-studio: `created: pre-commit, pre-merge-commit`;
  - tower-echo-brain: `created: pre-merge-commit`, `chained existing: pre-commit`.
- Verify:
  - post-commit is byte-identical to before;
  - running the installer again reports `already installed`.
- Rollback: restore the S1 hooks directory.
- Note: in governed repos, commits **fail closed while ATS is down**.

**S6 `[restart_gate]`.** None in Stage 1 (section 5). If one is ever added:

- Add it only after S2 and S3. On main, only the hooks read operator.toml
  (ats_context, session_registration, lockcheck), and all of them run from venv
  A. Neither ats-mcp nor ats-server reads it on main, so stale old MCP processes
  are not a constraint. The f5cb28b server does read it, and S3 upgrades the
  server.
- Rollback order: remove the table **before** any venv rollback. Old code with
  the table present gives exit 2 on **every edit and every prompt, in governed
  and ungoverned directories alike** (R7).

## 5. Restart gate in Stage 1: recommendation is no `[restart_gate]` table

Evaluated in-process with the f5cb28b loader (R5), and the hook was fed
payloads only (R4).

| Config | Hook (Claude Bash) | `ats safe-restart` / `--bootstrap` |
|---|---|---|
| **None (recommended)** | Raw `systemctl restart/stop/kill/try-restart...`, `service`, `kill`/`pkill` on all five units (anime-studio, tower-echo-brain, keyframe-blitz-worker, comfyui, comfyui-rocm): **exit 2**. The config warning shows only on service-control-like commands. | 503 fail closed for all five |
| Explicit `governed_units` subset | Units left out are **no longer guarded at all**. Example: listing only the two comfyui units gives anime-studio 422 "not governed", and the hook guards only the two. | as configured |
| Default set, only some units configured | as None | 503 for all five, because the whole config is invalid |

**Plainly: after Stage 1, no agent can restart any of the five units through
Claude's Bash tool or `ats safe-restart`. The operator restarts them from his
terminal.** That restart is ungated and not recorded in `service_restarts`
unless `record_restart` is used.

Configuring units honestly in Stage 1 is not possible:

- anime-studio has no `/api/system/inflight` yet: there is no `inflight.py` or
  route on deployed HEAD 6dda9b41. Its normal path is therefore always
  refused.
- A `[bootstrap]` table now would make **repeated** bootstrap restarts
  available, because the middleware never loads in Stage 1. Every one of them
  would carry the unclosed admission window. That contradicts Decision 2 (no
  bootstrap without a separate GO).
- tower-echo-brain and keyframe-blitz-worker have no in-flight, job or drain
  evidence. The only valid configs are a waiver on all four evidence classes,
  which amounts to an ungated restart with a written reason, or a probe that
  always fails.
- comfyui and comfyui-rocm could use the documented queue probe with three
  waivers. Because the config is all-or-nothing, that requires configuring the
  other three units as well (D3).

The proven `safe-restart` mechanics, on a disposable unit (R4):

- Success: lease granted, cleared, MainPID 920789 -> 977323.
- Refusal 409 when the GPU queue is busy, and 409 for `1 request(s) in flight:
  POST /work`. MainPID was unchanged in both cases.
- Rows: `authorized` + `completed`, then `refused` and `refused`. The leases
  ended `completed`, `refused` and `refused`.

## 6. What Stage 1 does NOT do

- No hold enforcement (#3579).
- No anime-studio change: no middleware, no drain, no gateway freeze.
- No ComfyUI admission boundary.
- No bootstrap restart.
- No sudoers or polkit boundary.
- No restart of any governed unit.
- Existing overlapping locks are not revoked.

## 7. Residual bypasses after Stage 1

| Path | Class | Evidence |
|---|---|---|
| New overlapping claim in a governed repo (REST or MCP, old or new MCP) | PREVENTED | 409 (R4, R8) |
| Zero-match, broad (more than 200 files) or outside-root pattern | PREVENTED | 422. A single new file under an existing directory is allowed by design. |
| Existing hazardous or overlapping locks | DOCUMENTED-ONLY | not revoked; preflight lists them |
| Claude Edit/Write on another session's or unclaimed file | PREVENTED | exit 2 |
| Claude Edit/Write while ATS is unreachable | DOCUMENTED-ONLY | the edit guard fails **open** (exit 0) |
| Claude Bash `sed -i`, redirect, `git merge`/apply/checkout etc. | PREVENTED | exit 2, and also while ATS is down |
| Claude Bash interpreters, `bash -c`/scripts, xargs, make etc. | DOCUMENTED-ONLY | `python -c` write: exit 0 with a warning |
| Non-ff merge or commit by anyone in the two repos | PREVENTED | pre-merge-commit / pre-commit exit 1 |
| `--no-verify`, ff merge, cherry-pick, rebase, am | DOCUMENTED-ONLY | the `--no-verify` commit landed with no record |
| `ATS_COMMIT_GUARD_OVERRIDE` | DETECTED | local audit log, which the user can edit |
| Borrowed `ATS_SESSION_ID` contradicting the session's pointer | PREVENTED | exit 2 |
| Same-account pointer or env identity; `actor_session_id` naming another live session | DOCUMENTED-ONLY | cooperative. The rehearsal itself dropped the owners' locks on the copy by naming their ids. |
| Agent Bash `systemctl`/`service`/`kill` of the five units | PREVENTED (hook only) | exit 2 |
| D-Bus (`busctl`), `bash script.sh`, Python subprocess, Codex, operator sudo, watchdogs and timers, `Restart=` policies | DOCUMENTED-ONLY | busctl and script: exit 0 |
| A guard that crashes, cannot start or times out | PREVENTED under the S4 array | blocks (R6). Today's lockcheck entry has no `onFailure`, so it fails open. |

## 8. Operator decisions needed

- **D1.** GO or NO-GO for Stage 1.
- **D2.** Install source: ff `main` to f5cb28b in /home/patrick/code/ai-team-sync
  (pipx metadata already points there), or a clean clone.
- **D3.** `[restart_gate]` in Stage 1: none (recommended), or all five units
  configured with signed waivers.
- **D4.** How 298b649f and 41e64e59 shed their hazard locks: `complete_session`,
  an own-lock DELETE with `actor_session_id`, or waiting for TTL.
- **D5.** Accept that agents cannot restart the governed units during Stage 1.
- **D6.** Accept `onFailure: block`: a broken install wedges edits and Bash
  until a manual fix from a terminal.
- **D7.** Accept the DOCUMENTED-ONLY residuals in section 7.

## 9. Recommendation: NO-GO as of 2026-10-14 04:18 UTC

Blocking items:

1. **P2.** Live repo_wide and broad hazards: preflight exits 1. Deploying now
   would refuse every new anime-studio claim.
2. Operator decisions D1-D3 and D6.

P1 is met: f5cb28b has a Codex PASS-WITH-CITATION (753b99aa) and an
owner-verified green suite.

**Conditional GO** once all of these hold:

- preflight shows only `matches_no_file` (or exits 0);
- P3-P6 are verified at the moment of deploy.

Everything else rehearsed clean:

- install and stamp;
- additive migration;
- old-on-new-DB rollback;
- the ordering hazard, with its failure mode demonstrated;
- the guard semantics, including Claude Code hook combination;
- old MCP against new REST. This is compatible except own-lock delete_lock,
  which already fails on production today. Delegation and message code is
  unchanged from main.

---

Evidence key (raw logs in the rehearsal scratchpad, `stage1/logs/`):

| Key | What it covers |
|---|---|
| R2 | migration and row counts |
| R3 | preflight, hazard 409s, owner release |
| R4 | hooks, git, uninstall, safe-restart |
| R5 | gate policy per config |
| R6 | `claude -p` hook semantics |
| R7 | rollback and ordering hazard |
| R8 | mixed MCP |
