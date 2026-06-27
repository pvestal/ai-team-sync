# Product gaps: orphan reaping + in-session scope updates

Surfaced 2026-06-27 while investigating "active" sessions holding locks for dead
Claude Code processes. Two related gaps in coordination lifecycle.

## Gap 1 — reaper can't detect a dead process (only idle timestamps)

`background_tasks.auto_complete_stale_sessions` completes an `active` session
only after `session_inactivity_hours` (default 12h) of no derived activity
(max of started_at / newest lock / commit / decision). There is **no liveness
signal**: a session whose Claude Code process died still looks active for the
full 12h window, and its locks linger until the 8h lock TTL. Presence
(`presence.py`, `STALE_SECONDS=30`) is in-memory and keyed by *developer*, not
session, so it cannot stand in for per-session liveness.

Observed impact: 2 sessions (agents `d6b26746`, `a9304739`) showed `active`
with held locks ~1.4h after their processes were gone. Proven dead by process
count (2 live `claude` procs for 4 "active" sessions) + stale transcripts.
Cleared manually via the HTTP API.

### Why not just shorten the window or hook a heartbeat (yet)

Both client hooks (`pre_tool_use_lockcheck`, `post_tool_use_presence`) fire
**only on Edit/Write/MultiEdit/NotebookEdit**, not on every tool. A read- or
bash-heavy live session emits no edits for long stretches, so an edit-only
heartbeat with a short window would falsely complete genuinely-active sessions.
A true liveness detector needs a periodic, tool-agnostic heartbeat that Claude
Code does not currently provide.

### Proposed safe design (deferred — behavioral change to a live server)

Backward-compatible, never worse than today:
1. Add nullable `Session.last_heartbeat`.
2. Add `POST /api/sessions/{id}/heartbeat` (bump to now).
3. Reaper: if `last_heartbeat` is set and older than
   `session_heartbeat_timeout_minutes` (new setting, e.g. 20) AND no newer
   lock/commit/decision -> auto-complete (fast path). If `last_heartbeat` is
   NULL -> unchanged 12h fallback. So heartbeating sessions get fast cleanup;
   everything else behaves exactly as now.
4. Client wiring: a tool-agnostic heartbeat (ideally a periodic/`PreToolUse-*`
   hook covering all tools, not just edits) POSTs the heartbeat.

Deferred deliberately: it changes reaper behavior on the running server (needs
a restart) and touches live every-edit hooks, while another session depends on
the board. Ship under review, not hot-patched mid-flight.

## Gap 2 — scope is frozen after start_session (from MCP) — SHIPPED

`start_session` is the only lock-creating MCP verb and is one-shot. There was
no MCP way to extend a running session's scope, even though the HTTP API already
supports it (`PATCH /api/sessions/{id}` updates scope text; `POST /api/locks`
adds an enforceable lock). So an agent whose work grew into new files was stuck
re-declaring a whole new session or dropping to raw HTTP.

**Fix (this commit):** new MCP tool `extend_scope(patterns, mode?)` — merges the
patterns into the current session's scope (de-duped) and creates enforceable
locks for them, reporting any that conflict with another active session. Thin
glue over the already-tested `GET /api/sessions/{id}`, `PATCH /api/sessions/{id}`,
and `POST /api/locks` endpoints; verified live against a running server.

Follow-up: add a dedicated `call_tool("extend_scope", ...)` integration test
(no existing harness mocks the MCP httpx client today).
