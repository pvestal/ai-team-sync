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

## Gap 0 — no auto-registration: a session that never called start_session was invisible — SHIPPED (2026-06-29)

**Was:** the presence/heartbeat/lock-guard hooks only MAINTAIN a session that was
created manually with `start_session`. The Stop heartbeat exits early ("no active
ATS session — nothing to heartbeat") when no pointer exists, and the PostToolUse
presence store evicts after 30s. So a live session that never called
`start_session` held no DB row, was absent from `team_status`, and held no
advisory locks — observed in the wild: a session edited the shared composition
layer for 2.5h while `team_status` reported an empty team. Tests were green
because they exercise the registered-session path; the failure was in the un-tested
seam (does a real session actually *issue* start_session — it's opt-in).

**Fix (shipped):** `hooks/session_autostart.py`, wired as a SessionStart hook (no
matcher → fires on startup/resume/compact). It creates a lightweight, **scope-less**
session row automatically (announces presence, claims no locks) and records the
per-session pointer (Gap 3). Idempotent: re-fires reuse the existing active session
via the pointer + a server status check; fail-open. Manual `start_session` /
`extend_scope` still layer real scope + locks on top. Covered by
`tests/test_session_autostart.py`.

## Gap 3 — `~/.ats_session` is a single global file — SHIPPED (2026-06-29)

**Was:** the MCP server persisted "the active session id" to one file in `$HOME`.
Two concurrent Claude sessions clobbered each other's pointer — last
`start_session` wins — so MCP verbs that read it (`complete_session`,
`log_decision`, `request_override`, and the heartbeat hook's fallback) could act
on the wrong session. Per-session identity (#1556) fixed the *board labels* but
not this pointer.

**Fix (shipped):** `session_pointer.py` keys the pointer by
`$CLAUDE_CODE_SESSION_ID` (present in every hook + the stdio MCP subprocess).
`save_pointer` writes both `~/.ats_session_<cid8>` (authoritative) and the legacy
global file (back-compat); `resolve_pointer` resolves `$ATS_SESSION_ID` → per-session
file → global. The MCP server's `save_session_id`/`load_session_id` and the
heartbeat hook all route through it, so concurrent sessions no longer cross-bump.
Covered by `tests/test_session_autostart.py` (per-session-beats-global-clobber +
env-override-wins).

## Gap 4 — `POST /api/locks` had no conflict check — SHIPPED (2026-09-14, #2756)

`create_lock` created the lock unconditionally, so `extend_scope`'s refusal branch
never fired and an extended scope could silently overlap another session's
exclusive lock. It now runs session creation's overlap check and refusal rule
(`_check_scope_conflicts` + `blocking_conflict`, excluding the session's own
locks) and answers 409 `scope_conflict`: refused when an overlapping lock of
another live session is exclusive, or when the request is exclusive and anything
overlaps; advisory over advisory still shares. `extend_scope` now takes locks
first and adds only granted patterns to the declared scope, reporting each
refusal with its reason (the holder, for a conflict). The check-then-insert is
not atomic, the rule's bidirectional fnmatch does not see every spelling (`src`
vs `src/**`), and repo roots are compared as strings after stripping a trailing
`/` (`/srv//repo` reads as a different repo), so the grant-time conservative
comparison in `authority.py` remains the authoritative exclusive-lock check for
mutation grants.

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
