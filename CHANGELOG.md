# Changelog

## [0.3.0] - 2026-03-27

### Added
- **MCP Server Integration** for Claude Code native support
  - 8 MCP tools: start_session, check_locks, request_override, respond_to_request, etc.
  - Auto-configuration via `~/.claude/config.json`
  - Real-time session management from within Claude
  - See MCP_SETUP.md for installation guide

### Changed
- Added `ats-mcp` command for running MCP server
- Added `mcp>=1.0.0` dependency
- Updated README with MCP quick start

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
