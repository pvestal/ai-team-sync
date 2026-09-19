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

With the server running, the sweep runs every 60 seconds. Locks on a session
that has heartbeated are released at most 21 minutes after its last derived
activity (20-minute threshold plus one sweep); for a session that never
heartbeated, the bound is 4 hours plus one sweep. An ATS outage suspends that
clock; startup cleanup runs when the server returns. Reaping deletes only the
completed session's lock rows, and a later session's claims stay in place.

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

## Gap 5 — the readers carried no caller identity, so a session was told its own claims blocked it — SHIPPED (2026-09-15, #2757)

`POST /api/git/pre-commit-check` (`routers/git_status.py`) accepts `staged_files`
and `repo_root` and nothing else. It carries no session identity, so it classifies
every covering exclusive lock as commit-blocking, the caller's own included. The
MCP `pre_commit_check` tool passes no session id either. Observed 2026-09-14
during #2756: a session was told "9 file(s) BLOCKED by exclusive locks ... Commit
will be blocked. Resolve conflicts first." — all nine were its own exclusive
claims.

The same missing-identity root cause reaches the **task brief**. `build_brief`
(`briefs.py`) selects every live lock overlapping the requested scope in the same
repo and lists it under "BLOCKERS NOW", with no exclusion of the calling session —
and its signature takes no session or agent argument, so it has no identity to
filter on. Because `start_session` creates the session's locks and *then* builds
its brief, a session is guaranteed to be shown its own brand-new locks as
blockers. Reproduced 2026-09-15 on session `048705db`: both locks it had just been
granted came back to it as "BLOCKERS NOW". This is the same defect as the
pre-commit one, not a second bug, and it is recorded on #2757.

**Reporting is not the defect.** `/api/locks/check` is a namespace reader; it is
asked "which live locks cover this path" and answering with the caller's own lock
is correct (`docs/lock-readers.md`). What is wrong is the **verdict** — "BLOCKED",
"Commit will be blocked", "BLOCKERS NOW" — rendered by a surface that does not
know who is asking. A fix belongs in the verdict, not in the reader's coverage
answer.

Enforcement status, re-verified 2026-09-15: in this repository the tool is
advisory text only. `.git/hooks` holds only `*.sample`, `core.hooksPath` is unset,
and `hooks/pre_commit.py` is installed in no repository. The only enforcing guard
on that path is the Claude PreToolUse lock-guard, which *does* self-exclude the
caller's session. So the severity is "teaches agents to distrust the tool", not
"blocks sanctioned commits" — re-check that judgement in any repo where the ats
hooks ARE installed (`scripts/install-hooks.sh`).

**Shipped in `c7a6334`.** One resolver, `caller_session.resolve_caller_session`,
answers "which session is asking" for BOTH readers, so a verdict cannot mean two
things depending on which endpoint rendered it. A caller is resolved only by
identifying itself — an explicit `session_id`, the `X-ATS-Session-Id` header, or
`X-ATS-Agent` when that label has exactly one active session for the account —
and every path is validated with `cross_account` against the kernel's owner of
the requesting socket, exactly as liveness validates its own headers. An
unresolved caller excludes nothing: it keeps the full conservative answer and is
told so, via `caller_identity_unresolved` and human-readable text.

Rejected, and recorded in the module docstring so it is not retried: "this uid
owns exactly one live session, so that must be the caller." A uid owning one
session is a coincidence, not proof the request came FROM it, and a bare `git
commit` hook owns none. It was caught by rows 3, 7 and 8b of
`test_lock_readers_lexical` and by `test_mcp_pre_commit_check` going green when
they should have been red — a real caller being told a foreign exclusive lock did
not block it.

Blocker diagnostics now name the agent and the WHOLE session id. One human runs
many agents, so the developer name cannot say whose lock it is; it stays as
display, never as identity.

The trust granularity is the OS account, and this is a limit rather than a
guarantee: within one account a caller can name a sibling session and drop its
locks from ITS OWN answer only — no lock is released, no claim taken, no mutation
granted. That is #2741's own boundary; it is not widened here, and `cross_account`
is unchanged.

Follow-up `ec9d97c` (tests only, no production change): the two new `start_session`
tests inherited the developer shell's `ATS_AGENT`/`CLAUDECODE` identity, so they
passed locally and failed CI as `restricted`. They now declare the identity each
assertion depends on. CI green on `ec9d97c`: 1013 passed, 23 skipped.

## Gap 6 — a resurrected session keeps its scope and silently loses its locks — OPEN (#2760)

`session_heartbeat_timeout_minutes` is 20 (`config.py`). A session silent past
that is completed by the reaper, which deletes its locks (`background_tasks.py`:
select `ScopeLock` where `session_id == sess.id`, then `db.delete(lock)`). A later
heartbeat resurrects it: `routers/sessions.py` restores `status`, `completed_at`,
`auto_completed` and the summary marker — but nothing restores the locks. The
board then shows an ACTIVE claim, with its declared scope intact, holding nothing.

**This is a regression, and the two halves were written fifteen days apart.**
Resurrection landed in `b99ddc3` (2026-08-10), when reaping did *not* release
locks — a reaped session kept them for the full `lock_ttl_hours`, so there was
nothing for resurrection to restore and the path was coherent. `06d91b9`
(2026-08-25, "reaping a session now actually frees its lane") correctly fixed that
lingering-lane bug, and in doing so gave resurrection something to put back. The
resurrect branch was never updated to match.

The path is not an edge case: the reaper's own comment records that a long-running
interactive session is routinely silent past the window, so "reap -> heartbeat ->
resurrect is the NORMAL cycle". Its measurement — 2026-08-14 over 1116 live rows,
16 sessions carrying thrash, 55 resurrect events, worst single session 10 cycles —
says how often the path fires, *not* how many locks were lost, because it predates
the 2026-08-25 change that created the loss. The blast radius is resurrect events
since 06d91b9, which has not been counted.

One real scope limit: an identity-bound session is refused resurrection outright
with 409 (`bound_worker`, #2741 — its authority must not outlive it). The defect
therefore reaches only sessions with no identity binding.

Observed 2026-09-14 during the #2759 canary: session `08353591` (agent
`claude-code:6ad57ba1`) started with 3 advisory locks; heartbeats at 18:10:09 then
18:49:05, a 39-minute gap across a long observation wait. At 18:49 `team_status`
showed it active with scope intact and "Locks: 0"; `scope_locks` held no rows for
it and the server journal has no DELETE /api/locks call, so the reaper removed
them, not the owner. Its summary reads "[resurrected: heartbeat proved the reap
wrong]". For that canary it changed nothing — those locks were deliberately
advisory, and advisory locks never block grants.

Impact: the owner believes it holds its claims while every reader
(`check_locks`, `whos_editing`, the PreToolUse guard) sees nothing. An exclusive
claim would stop protecting its files, and stop blocking mutation grants, with no
signal to the owner.

Fix direction (not decided): on resurrection either restore the reaped locks under
a conflict check (another session may have taken the lane meanwhile), or resurrect
into a visibly lockless state and tell the owner on its next tool call. At minimum
`team_status` should say "resurrected, locks released" instead of showing intact
scope. Tests: reap by silence, then heartbeat — the owner is told its locks are
gone, or they are restored without overlapping a newer claim.

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
