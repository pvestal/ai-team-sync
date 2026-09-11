# Agent Integration Status

ai-team-sync is no longer a manual-only coordination layer. It has working CLI,
REST, dashboard, VS Code, Claude Code MCP, and Claude Code hook integrations.

## Claude Code

Claude Code is the most complete integration target:

- `ats-mcp` exposes session, lock, override, decision, git-status, and presence
  tools.
- `session_autostart.py` makes new Claude sessions visible even before they
  declare a real work scope.
- `pre_tool_use_lockcheck.py` blocks edits inside another active session's scope
  and, for coordinated repos, also blocks unclaimed edits by the current session.
- `post_tool_use_presence.py` publishes active file presence after edits.
- `session_heartbeat.py` keeps live sessions from being reaped and lets dead
  sessions release locks quickly.
- `override_inbox.py` surfaces pending override requests without manual polling.

This is the reference integration pattern for any agent that supports hooks.

## Codex

Codex can use ai-team-sync today through the CLI and REST API. The Tower
operator guardrails require:

- `ATS_AGENT=codex`
- `ATS_SERVER_URL=http://localhost:8400`
- `ats session start` at task start
- `ats lock check <path>` before file edits
- `ats decision list --all` before relying on prior work
- `ats session complete` at wrap-up

The open gap is enforcement: Codex does not currently have a configured
PreToolUse-style hook that automatically blocks edits when the lock check is
missing or conflicting. Until Codex has that hook, use a launcher or wrapper for
high-risk repos so the procedure is hard to skip.

## Cursor, Aider, Copilot, and Local Models

Other agents should integrate in one of three ways:

- CLI wrapper: set `ATS_AGENT=<agent-name>`, start a scoped session, and run
  `ats lock check` before edits.
- REST client: call `/api/sessions`, `/api/locks/check`, `/api/presence`, and
  `/api/decisions` directly.
- Editor integration: use the VS Code extension for human-visible warnings and
  status.

For agents without reliable hooks, prefer exclusive locks for shared service
files and work from isolated git worktrees.

## Current Operational Priorities

1. Keep the server bound to localhost by default because the write API is
   unauthenticated.
2. Keep MCP entrypoint smoke tests in CI so dependency upgrades cannot silently
   remove the tool surface.
3. Build Codex/other-agent wrappers that make session start, lock check, and
   completion automatic.
4. Use repo-root anchored scopes when working across multiple repositories.
5. Treat advisory locks as visibility, not enforcement, unless the client has a
   blocking pre-edit hook.
