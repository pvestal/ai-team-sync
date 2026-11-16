# #3677 rollout (prepared, not performed)

Nothing in this branch is deployed by the branch itself. The operator's
coordinator runs this in a quiet window; the worker that wrote it did not
reinstall or restart anything.

## What changes where

| Component | Change | Picks it up |
|---|---|---|
| `session_pointer.py` | agent pointers (`~/.ats_agent_pointers/`), tool-caller records (`~/.ats_tool_callers/`), `open_capability_sessions` | every process after the pipx reinstall |
| new hook `hooks/pre_tool_use_ats_caller.py` | records tool_use_id -> agent_id for `mcp__ai-team-sync__.*` calls | Claude processes STARTED after the settings change (Claude Code snapshots hooks at startup) |
| `scripts/install-claude-hooks.py` | adds that PreToolUse group (matcher `mcp__ai-team-sync__.*`, timeout 5, no `onFailure`) | run by `scripts/deploy.sh` |
| `mcp/server.py`, `mcp/caller_context.py` | subagent start_session, subagent default target | each Claude process's own `ats-mcp`, i.e. after that client restarts |
| `hooks/commit_guard.py`, `pre_tool_use_bashguard.py`, `pre_tool_use_lockcheck.py` | agent identity, `env_held`, sibling rule in git + Bash guards, #2893 substring removed | next hook / git invocation, including in already-running clients |

No REST route, schema, migration or database change. `ats-server` imports only
unchanged functions from the changed modules (`commit_guard.governed`,
`lockcheck.scope_matches`), so its restart is not functionally required;
`deploy.sh` restarts it anyway so `/api/version` and `ats_version` agree.

## Procedure (quiet window)

`pipx install --force` replaces the venv every hook runs from. While it runs
(tens of seconds under load) a hook command can fail to start; the edit guard
and Bash guard are wired `onFailure: block`, so agents see blocked edits and
Bash calls for that window and should simply retry. The `ats-server` restart is
~2 s during which the Bash and commit guards fail CLOSED in governed repos and
MCP calls fail. Hence a quiet window.

```bash
# 0. preconditions
cd /home/patrick/code/ai-team-sync
git status --short --untracked-files=no      # must be empty
git log -1 --oneline                          # note PREV=<sha> for rollback
cp ~/.claude/settings.json ~/.claude/settings.json.pre-3677

# 1. land the branch (fast-forward or merge, per the release train)
git merge --ff-only fix/3677-subagent-session-identity   # or the train's merge

# 2. deploy: stamp, pipx reinstall, install Claude/Codex hooks, restart ats-server
scripts/deploy.sh

# 3. verify the install
curl -s http://localhost:8400/api/version | python3 -m json.tool   # commit = new HEAD
grep -c pre_tool_use_ats_caller ~/.claude/settings.json            # 1
diff <(python3 -m json.tool ~/.claude/settings.json.pre-3677) \
     <(python3 -m json.tool ~/.claude/settings.json)              # only the new group
```

## Post-deploy verification (live)

In a Claude Code session STARTED after step 2 (old sessions keep the old
`ats-mcp` and the old hook snapshot):

1. `ats_version` reports MCP and REST on the new commit.
2. Note `cat ~/.ats_session_$CLAUDE_CODE_SESSION_ID` (the parent's pointer).
3. The main thread starts its own session, then spawns a subagent that calls
   `start_session` WITHOUT `parent_session_id`. Expect in the result:
   "Registered as THIS SUBAGENT's own session (Claude Code agent <id>, subagent
   of session <parent>)".
4. `cat ~/.ats_session_$CLAUDE_CODE_SESSION_ID` is unchanged;
   `cat ~/.ats_agent_pointers/$CLAUDE_CODE_SESSION_ID/<agent id>` names the
   subagent's session; `ls ~/.ats_tool_callers/` shows recent records.
5. The subagent edits and commits a file its own session locked in a governed
   repo without any `save_pointer`; the commit prints the non-blocking
   "held by another session of this same Claude session" note only when the
   committing pointer is the parent's.
6. The parent's `log_decision` without `session_id` lands on the parent; the
   subagent's lands on the subagent's session.
7. If step 3 instead says the session became the default: the hook did not
   record (check `~/.ats_tool_callers/` and the settings group). The guard-side
   fixes (sibling rule, `env_held`) still apply.

## Rollback

```bash
cd /home/patrick/code/ai-team-sync
git checkout "$PREV"        # or revert the merge on main, then check out main
scripts/deploy.sh           # old code; its installer leaves the new group in place
# remove the new PreToolUse group (otherwise each ATS MCP call logs a harmless
# "No module named ai_team_sync.hooks.pre_tool_use_ats_caller" hook error):
python3 - <<'PY'
import json, os, tempfile
p = os.path.expanduser("~/.claude/settings.json")
s = json.load(open(p))
groups = s.get("hooks", {}).get("PreToolUse", [])
s["hooks"]["PreToolUse"] = [g for g in groups if not any(
    "pre_tool_use_ats_caller" in str(h.get("command", "")) for h in g.get("hooks", []))]
fd, tmp = tempfile.mkstemp(dir=os.path.dirname(p), prefix=".settings.")
with os.fdopen(fd, "w") as f:
    f.write(json.dumps(s, indent=2) + "\n")
os.replace(tmp, p)
PY
```

Old code ignores `~/.ats_agent_pointers/` and `~/.ats_tool_callers/` (session
ids and agent ids only, no capabilities); deleting them is optional. A session a
subagent started under the new code is in the keyring as usual and completes
normally under the old code by naming its `session_id`.
