# MCP Tool Status

This file tracks the current ai-team-sync MCP surface for agent coordination.
It replaces the original gap inventory, which predated the complete MCP server.

## Current MCP Tools

The Claude Code MCP server currently exposes the core coordination tools:

- `start_session`
- `extend_scope`
- `check_locks`
- `whos_editing`
- `request_override`
- `check_pending_requests`
- `check_my_override_requests`
- `respond_to_request`
- `get_override_request_details`
- `team_status`
- `list_all_locks`
- `get_session_details`
- `pause_session`
- `resume_session`
- `complete_session`
- `log_decision`
- `get_decision_history`
- `check_git_changes`
- `pre_commit_check`
- `delete_lock`

Keep `tests/test_mcp_entrypoint_smoke.py` in the required test set for any
dependency or packaging change. It catches the practical outage class: the MCP
entrypoint imports but exposes no useful tools to agents.

## Remaining Gaps

### Codex and Other Agents

Claude Code has native MCP and hook coverage. Codex, Cursor, Aider, and similar
agents can use the CLI or REST API, but they do not automatically run the same
pre-edit guard unless launched through a wrapper or their own hook integration.

Recommended next step: provide a small per-agent launcher that exports
`ATS_AGENT`, starts a scoped session, and requires `ats lock check` before edits.

### Real-Time Event Consumption

The server emits events and the dashboard can show live state, but MCP clients
still mostly interact through explicit tool calls. Override inbox hooks reduce
polling for Claude Code; other clients need equivalent event or prompt-injection
support.

### Access Model

The ATS HTTP API is unauthenticated. Keep the server bound to `127.0.0.1` by
default. Use `ATS_HOST=0.0.0.0` only for a deliberately trusted network setup.

### Policy

Advisory locks are useful for normal parallel work, but sensitive lanes should
use exclusive locks: shared service import paths, migrations, deploy/restart
windows, and monolith files with high collision risk.
