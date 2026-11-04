# #3593 rollout analysis (written, not performed)

Nothing in this branch has been deployed. This is what deploying it would take,
traced from how each changed module is actually loaded on tower (checked
2026-11-04, read-only).

## How the code is loaded

| Component | Launched by | Lifetime | Picks up a reinstall |
|---|---|---|---|
| `ats-server` | user unit `ats-server.service`, `ExecStart=~/.local/bin/ats-server` | long-running | only on restart |
| `ats-mcp` (stdio) | each Claude Code process (`~/.claude.json` `mcpServers`, `~/.local/bin/ats-mcp`) and each Codex process (`~/.codex/config.toml`) | one per client session, for its whole life | only when that client restarts / reconnects MCP |
| Claude hooks | `~/.claude/settings.json`: `<pipx venv>/bin/python -m ai_team_sync.hooks.{ats_context,override_inbox,pre_tool_use_bashguard,pre_tool_use_lockcheck,pre_tool_use_restart_guard,session_autostart,session_heartbeat}` and `~/.local/bin/ats-presence-hook` (`post_tool_use_presence`) | one process per event | next event |
| Codex hooks | `~/.codex/hooks.json`: `codex_session_autostart`, `ats_context` | per event | next event |
| git hooks | `<repo>/.git/hooks/*-ats` runners, `exec <pipx python> -m ai_team_sync.hooks.<mod>` (`git_hook_install`) | per git invocation | next invocation |
| `ats` CLI | per invocation | per invocation | next invocation |

Every one of them runs from the pipx venv `~/.local/share/pipx/venvs/ai-team-sync`,
which is a non-editable COPY (`direct_url.json`: `file:///home/patrick/code/ai-team-sync`,
`dir_info: {}`), stamped `COMMIT = "1a3ed91"`, `DIRTY = True`,
`BUILT_AT = 2026-10-31T13:06:09-07:00`. None of the #3593 code is in it (no
`_IN_PROCESS_TOKENS`, no `CAPABILITY_DIR_NAME`). The worktree is not what any
client runs, so merging alone changes nothing live.

## What each changed module needs

| Module | Loaded by | Needs |
|---|---|---|
| `session_pointer.py` (keyring, full-id pointers, migration read) | MCP, CLI, every hook | pipx reinstall; MCP reconnect for the MCP side |
| `mcp/server.py` (in-process map, targeting, subagents, delete_lock/reconcile actor) | `ats-mcp` | pipx reinstall + **restart every Claude/Codex client** (or reconnect MCP) |
| `cli.py` | `ats` | pipx reinstall |
| `hooks/*` (lockcheck sibling rule, keyring errors, commit_guard pointer read) | per event | pipx reinstall; no reload |
| `git_utils.py` (worktree/submodule root; prior commits on this branch) | hooks AND `ats-server` (`routers/sessions`, `routers/authority`, `routers/git_status`, `background_tasks`) | pipx reinstall + **`ats-server` restart** |
| `session_target.py` | docstring only | none |

No REST route, schema or database column changes. The MCP now sends
`X-ATS-Approval-Token` on `DELETE /api/locks/{id}` and
`POST /api/delegations/{id}/close`, which the current server ignores, so an old
server with new clients (or the reverse) is compatible.

The documented path (`scripts/deploy.sh`, run in the main checkout after the
merge) does all of: stamp, `pipx install --force '.[console]'`, re-install the
Claude/Codex hooks, `systemctl --user restart ats-server`. It does not restart
clients; it prints that they must be restarted.

## Live-session impact

Between the reinstall and each client's restart, a running Claude session has
an OLD `ats-mcp` and NEW hooks.

* **Existing `~/.ats_approval_<cid8>` files.** New code reads them as a
  migration path, for exactly the session the file itself names, and never
  writes or deletes them. An old `ats-mcp` keeps writing them (one pair,
  O_TRUNC), so during the window a running session has the same single-pair
  behaviour it has today; nothing is lost that is not already lost on main.
* **Existing `~/.ats_session_<cid8>` pointers.** New hooks read them when no
  full-id pointer exists, so running sessions keep their identity in the edit
  guard, commit guard and prompt hook.
* **Divergence risk.** The first NEW SessionStart (or prompt-hook
  registration) in a running client writes a full-id pointer. If the OLD
  `ats-mcp` then starts another session, it writes only the legacy pointer, and
  new hooks keep preferring the full-id one, which names the earlier session.
  The edit guard would then judge edits as the earlier session ("holds no live
  lock") until that client restarts. Mitigation: deploy while sessions are
  quiet and restart every Claude/Codex client right after the deploy.
* **Sibling grant.** Parallel subagents started by an OLD `ats-mcp` have only
  the latest capability in the legacy file, so only that one counts as a
  sibling. Deployed main has no sibling rule at all, so this is not a
  regression against what runs today.
* **Capabilities are never printed.** The keyring holds secrets: 0700
  directory, 0600 files; nothing logs a value.

## Rollback

Re-deploy the previous commit from the main checkout (`git checkout 1a3ed91`,
or the commit then current, followed by `scripts/deploy.sh`) and restart
clients. Old code reads only the eight-character names: it ignores
`~/.ats_capabilities/` and the full-id `~/.ats_session_<client id>` files.
Consequence: a session STARTED under the new code has its capability only in
the keyring (and in its new MCP process's memory, which a client restart
loses), so an old-code process cannot complete it; it ends through the reaper,
which is exactly today's failure mode. Complete open sessions before rolling
back. Deleting `~/.ats_capabilities/` afterwards is optional cleanup; it
contains live capabilities, so delete rather than copy it.
