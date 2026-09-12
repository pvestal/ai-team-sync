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

Codex reaches ai-team-sync over **MCP**, as well as through the CLI and REST
API. For a Codex session that starts itself, the Tower operator guardrails
require:

- `ATS_AGENT=codex`
- `ATS_SERVER_URL=http://localhost:8400`
- `ats session start` at task start
- `ats lock check <path>` before file edits
- `ats decision list --all` before relying on prior work
- `ats session complete` at wrap-up

The open gap is enforcement **for a self-started Codex session**: it has no
configured PreToolUse-style hook that blocks edits when the lock check is
missing or conflicting. Until it does, use a launcher or wrapper for high-risk
repos so the procedure is hard to skip.

**A DELEGATED Codex session is different, and that gap is closed.** `delegate
--worker codex` launches `codex exec` with `--sandbox read-only` for READ_ONLY
and VERIFY, which Codex's own runtime enforces — a write attempt is refused by
the sandbox, not by instruction. The parent also records the absolute binary it
resolved at spawn, so the record cannot claim Codex ran when something else did.
See [docs/delegation.md](docs/delegation.md).

One Codex-specific detail matters when wiring this: Codex starts its MCP servers
from its own config, and a declared `[mcp_servers.*.env]` block replaces the
inherited environment rather than extending it. The ATS isolation keys are
therefore passed as per-invocation `-c` overrides. Without them a delegated
child's ATS client falls back to the shared pointer and reports its parent's
session as its own.

## Cursor, Aider, Copilot, and Local Models

Other agents should integrate in one of three ways:

- CLI wrapper: set `ATS_AGENT=<agent-name>`, start a scoped session, and run
  `ats lock check` before edits. `ATS_AGENT` names the session; it is not
  evidence of which worker ran a delegation.

To be a **delegation target** an agent additionally needs an entry in the launch
specification: its executable, how it takes a prompt, and its per-mode
enforcement flags. Without one, delegating to it fails closed rather than
launching it unrestricted — deliberately, since a mode whose enforcement cannot
be expressed for a worker is a mode that worker does not support. Adding Cursor
means adding that entry, not just a new `ATS_AGENT` value.
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
3. Build wrappers that make session start, lock check and completion automatic
   for **self-started** agent sessions. Delegated sessions already get this:
   the launcher applies the mode's enforcement and ATS creates the child's
   session record.
4. Use repo-root anchored scopes when working across multiple repositories.
5. Treat advisory locks as visibility, not enforcement, unless the client has a
   blocking pre-edit hook.
