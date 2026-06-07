# Changelog

## [Unreleased]

### Added
- **Multi-agent identity**: `ATS_AGENT` env var explicitly sets the agent for any
  tool (e.g. `ATS_AGENT=codex`, `ATS_AGENT=ollama:<model>`); best-effort Codex
  auto-detection via `CODEX_*` env signature.
- **`ats decision list --all`**: read the whole team's decision log, not just the
  active session's.
- `AGENTS.md` contributor guide; tests for agent detection and decision listing.

### Changed
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
