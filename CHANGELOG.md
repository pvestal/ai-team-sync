# Changelog

## [Unreleased]

### Added
- **Session liveness heartbeat (reaper Gap 1)**: nullable `Session.last_heartbeat`,
  `POST /api/sessions/{id}/heartbeat`, and a client `session_heartbeat.py` hook
  (wire as a per-turn `Stop` hook — tool-agnostic). A session that heartbeats and
  then goes silent for `session_heartbeat_timeout_minutes` (default 20) is reaped
  fast and its locks released, instead of a dead Claude process holding the lane.
  Sessions that never heartbeat keep the (now shorter) fallback window — never-worse.
- **Explicit startup cleanup sweep**: the server runs one lock/override/session sweep
  immediately on startup (`run_startup_cleanup`), so a restart promptly reclaims
  sessions/locks orphaned while it was down.
- **Override-inbox hook** (`override_inbox.py`, wire as `UserPromptSubmit`): injects
  pending override requests targeting your locks into the turn context, so the unlock
  handshake no longer needs polling or a human relay. Owner-only, fail-open.
- `last_heartbeat` is now surfaced on `SessionResponse` (diagnose phantom-active rows).
- Integration test for the `extend_scope` MCP tool (routes the MCP httpx client into
  the in-process ASGI app — the previously-missing harness).
- **Multi-agent identity**: `ATS_AGENT` env var explicitly sets the agent for any
  tool (e.g. `ATS_AGENT=codex`, `ATS_AGENT=ollama:<model>`); best-effort Codex
  auto-detection via `CODEX_*` env signature.
- **`ats decision list --all`**: read the whole team's decision log, not just the
  active session's.
- `AGENTS.md` contributor guide; tests for agent detection and decision listing.

### Fixed
- **`pre_commit_check` MCP tool was a silent no-op**: it sent `{"paths": ...}` while
  the endpoint reads `staged_files`, and parsed `blocked`/`warned` while the endpoint
  returns `blocking_locks`/`advisory_locks`. The argument was dropped (server
  auto-detected staged files from its own cwd) and the response never matched, so it
  always reported "clear". Now wired correctly and tested end-to-end.
- **`whos_editing` was blind to a concurrent same-developer session**: presence was
  keyed by developer name (so two sessions of one git user clobbered each other) and
  exclusion was by developer. Presence is now keyed by `(developer, agent)`,
  `whos_editing` excludes by session via `exclude_agent`, and the presence hook emits
  a per-session agent label (from the PostToolUse `session_id`). A parallel session of
  the same person is now visible.

### Changed
- **Reaper fallback window** for non-heartbeating sessions cut from 12h to 4h
  (`session_inactivity_hours`), so dead lanes don't sit parked all day.
- **Security:** `ATS_HOST` now defaults to `127.0.0.1` (was `0.0.0.0`); the write
  API is unauthenticated and should not bind all interfaces by default.
- `uvicorn` no longer runs with `reload=True` in production (`ats-server`).

### Removed
- Dropped a bundled, environment-specific `vision-qa` MCP plugin from the public
  package (it belonged to a private deployment).

## [0.2.0] - 2026-03-27

### Added
- **Overlap detection at session start**: System now detects scope conflicts BEFORE creating sessions
- **Exclusive lock mode**: New `--exclusive` flag for session start to block all overlapping work
- **Bidirectional pattern matching**: Detects conflicts when patterns overlap in either direction
- **Better error messages**: Clear 409 responses explaining exactly which locks conflict
- **Conflict documentation**: Added `examples/conflict-scenarios.md` with real-world examples

### Fixed
- **Critical bug**: Advisory locks now properly warn about overlaps (previously allowed silently)
- **Exclusive mode enforcement**: Exclusive lock requests are blocked by ANY existing lock
- **Lock mode propagation**: Session `lock_mode` parameter now properly applied to created locks

### Changed
- Updated README with lock mode documentation and examples
- CLI now shows clear error messages for lock conflicts

## [0.1.0] - 2026-03-27

### Added
- Initial release
- Session management with scope-based locking
- Advisory lock mode (default)
- Decision logging
- Team status visibility
- Slack/Telegram notifications
- VS Code extension
- GitHub Action for PR enrichment
