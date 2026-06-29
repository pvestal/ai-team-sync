# Coordination lifecycle: gaps, fixes, and the attention model

Surfaced 2026-06-27..29 while running multiple Claude sessions against shared
repos: "active" sessions holding locks for dead processes, frozen scope, and the
question of how attentive an agent must actually be to coordinate.

## Gap 1 — reaper couldn't detect a dead process — SHIPPED (2026-06-29)

**Was:** `auto_complete_stale_sessions` completed an `active` session only after
`session_inactivity_hours` (then 12h) of no *derived* activity (max of started_at /
newest lock / commit / decision). There was no liveness signal, so a session whose
Claude process died still looked active for the full window and its locks lingered
up to the 8h lock TTL. Observed: 2 sessions showed `active` with held locks ~1.4h
after their processes were gone; cleared manually via the HTTP API.

**Fix (shipped):**
1. `Session.last_heartbeat` (nullable; idempotent column migration in `database.py`).
2. `POST /api/sessions/{id}/heartbeat` bumps it to now.
3. Reaper has two windows: a **fast path** (default 20m,
   `session_heartbeat_timeout_minutes`) that applies **only** to sessions that have
   ever heartbeated, and the **fallback** (`session_inactivity_hours`, now **4h**)
   for sessions that never did. So heartbeating sessions get fast cleanup and
   everything else is never-worse — just a shorter fallback than before.
4. Client `hooks/session_heartbeat.py`, wired as a per-turn `Stop` hook
   (tool-agnostic: read/bash-only turns still count as alive, the reason an
   edit-only heartbeat was rejected). Fail-open.
5. Explicit `run_startup_cleanup()` sweep on server boot, so a restart promptly
   reclaims sessions/locks orphaned while the server was down.

Why a tool-agnostic `Stop` hook (not the edit hooks): the lock-guard and presence
hooks fire only on Edit/Write/MultiEdit/NotebookEdit. A read- or bash-heavy live
session emits no edits for long stretches, so an edit-only heartbeat plus a short
window would falsely reap a genuinely-active session. `Stop` fires once at the end
of every assistant turn regardless of tools used — exactly the liveness signal.

## Gap 2 — scope frozen after start_session — SHIPPED (2026-06-27)

`extend_scope(patterns, mode?)` merges patterns into the running session's scope
(de-duped) and creates locks for them. Thin glue over `GET/PATCH /api/sessions/{id}`
and `POST /api/locks`. Now has an integration test (`tests/test_mcp_extend_scope.py`)
that routes the MCP httpx client into the in-process ASGI app.

## Gap 3 — `~/.ats_session` is a single global file (OPEN)

The MCP server persists "the active session id" to one file in `$HOME`. Two
concurrent Claude sessions on the same machine clobber each other's pointer — the
last `start_session` wins — so MCP verbs that read it (`complete_session`,
`log_decision`, `request_override`, and the heartbeat hook's fallback) may act on
the wrong session. Per-session identity (#1556) fixed the *board labels* but not
this pointer. Mitigation today: set `ATS_SESSION_ID` per session (the heartbeat
hook honors it first). Real fix (deferred): key the pointer by Claude session id,
or have the MCP server resolve the session from the agent label.

## Gap 4 — `POST /api/locks` has no conflict check (OPEN)

`create_lock` creates the lock unconditionally (only validates the session exists
and the pattern is a glob). It never returns 409. So `extend_scope`'s "not locked —
held by another active session" branch is effectively dead: an extended scope can
silently overlap another session's locks. The PreToolUse lock-guard still catches
the actual edit, so this is a *reporting* gap, not a safety hole — but the tool
output is misleading. Fix: have `create_lock` run the same overlap check as
`create_session` and return 409 on exclusive conflicts.

## The attention model — how much must an agent actively monitor?

Short answer: for the common case, **almost none** — coordination is enforced by
hooks at turn/edit boundaries, not by the agent remembering to poll. The one place
that still needs active attention (or a human relay) is the override handshake.

Automatic, zero-attention (with hooks wired):
- **SessionStart** health check prints the active sessions + locks at the top of
  every new session — the agent starts already knowing the board.
- **PreToolUse lock-guard** blocks an edit into another session's scope at the
  moment of the edit. The agent cannot clobber blind even if it never checked.
- **PostToolUse presence** broadcasts what the agent touches; **Stop heartbeat**
  proves liveness. Both are passive.
- **Reaper + startup sweep** clean up dead sessions/locks on their own.

Needs active attention or a human in the loop:
- **Override requests** (`request_override` → owner `respond_to_request`). Nothing
  pushes a pending request *into* a busy owner agent's turn — Claude Code has no
  inbound interrupt mid-session. Today the owner only learns of it by calling
  `check_pending_requests`, or because the Slack/Telegram dispatcher alerted the
  **human**, who relays it. Two designed-in mitigations keep this from wedging:
  requests **auto-expire** in 15m (the requester isn't blocked forever), and
  auto-approval keywords (`urgent`/`security`/`hotfix`/`critical`) let a requester
  self-unblock for genuine cases without the owner.

This handshake is the remaining active-attention item — nothing pushes a pending
request into a busy owner's turn; the owner polls `check_pending_requests` or the
human relays the Slack/Telegram alert.
