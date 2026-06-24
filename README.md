# ai-team-sync

Stop AI agents (and humans) from stepping on each other's work.

When two devs both tell their AI agents to change the same files, nobody knows until conflicting PRs appear. ai-team-sync gives instant visibility — who's working on what, and why — through declared **sessions**, file **scope locks** (advisory or exclusive), and logged **decisions**. It surfaces in VS Code, a browser dashboard, a CLI, and natively in Claude Code via MCP.

> Status: built as a personal multi-agent coordination tool, used daily. Small, dependency-light, MIT-licensed — useful if you run more than one coding agent against the same repo.

## Requirements

- Python **3.11+**
- SQLite (bundled) for local use, or Postgres (`asyncpg`) for a shared server

## Setup

```bash
./setup.sh
```

That's it. Installs everything, starts the server, installs the VS Code extension.

### Manual install (from source)

```bash
pip install -e .            # or:  pip install -e ".[dev]"  to run the tests
ats-server                  # starts the API + dashboard on :8400
pytest                      # run the test suite (needs the [dev] extra)
```

Console entry points: `ats` (CLI), `ats-server` (API/dashboard), `ats-mcp` (MCP server for Claude Code).

## How to use

**VS Code** (primary interface):

- `Ctrl+Shift+P` → **AI Team Sync: Run Demo** — see what the notifications look like
- `Ctrl+Shift+P` → **AI Team Sync: Start Session** — tell your team what you're working on
- `Ctrl+Shift+P` → **AI Team Sync: Complete Session** — release locks, notify team
- Status bar (bottom-left) shows active session count — click for details
- Save a locked file → automatic warning toast

**Browser** (for remote teammates or quick glance):

```
http://YOUR_SERVER:8400/dashboard
```

### MCP Server for Claude Code

Enable Claude Code to natively use ai-team-sync:

```json
// Add to ~/.claude/config.json
{
  "mcpServers": {
    "ai-team-sync": {
      "command": "ats-mcp",
      "env": {
        "ATS_SERVER_URL": "http://localhost:8400"
      }
    }
  }
}
```

Claude can now automatically check locks, request overrides, and coordinate with your team!

See [MCP_SETUP.md](MCP_SETUP.md) for full instructions.

### Use the CLI

```bash
# Start a session — team gets notified
ats session start -s "src/auth/**" -d "Refactoring auth to use JWT"

# Start with exclusive lock (blocks all overlapping work)
ats session start -s "src/auth/**" -d "Critical auth refactor" --exclusive

# Lock a path with a reason (the WHY is shown to anyone it blocks)
ats lock add "src/auth/**" --mode exclusive --reason "JWT migration, #1234"

# Check if a file is locked by someone else
ats lock check src/auth/middleware.py

# Log a design decision
ats decision log "Chose JWT over sessions" \
  -c "JWT" -r "session cookies" \
  --reason "Stateless auth needed for horizontal scaling"

# See what the team is working on
ats team
ats session complete -m "Done"
```

### Works with any agent

Each session records *which agent* created it, so `ats team` shows Claude Code
vs Codex vs Cursor at a glance. Identity resolves from the `ATS_AGENT` env var —
set it for any agent (`ATS_AGENT=codex`, `ATS_AGENT=ollama:qwen2.5-coder`) — and
falls back to auto-detecting known agents. Read what other agents have decided
with `ats decision list --all`.

## Lock Modes

**Advisory Mode (default)**: Warns about conflicts but allows overlapping work
- Use for parallel work on related files
- Team members get notifications about overlaps
- Example: Two devs working on different auth components

**Exclusive Mode**: Blocks all overlapping sessions
- Use for critical refactoring or migrations
- Prevents any conflicts during sensitive work
- Example: Database schema migration, major API changes

```bash
# Advisory (default) - allows overlap with warnings
ats session start -s "frontend/**" -d "UI updates"

# Exclusive - blocks any overlapping work
ats session start -s "backend/database/**" -d "Schema migration" --exclusive
```

## What happens

1. You start a session → teammates see a toast in VS Code + dashboard updates
2. Your file patterns are locked → teammates get warned if they try to edit those files
3. You log decisions (why approach X over Y) → persists after the chat session ends
4. You complete → locks release, team gets notified

## Remote access

By default the server binds to `127.0.0.1:8400` (localhost only) — the write API
is **unauthenticated**, so don't expose it to untrusted networks. To share it
across a *trusted* network, set `ATS_HOST=0.0.0.0` deliberately. Then any machine
on that network can:
- Open the dashboard in a browser
- Point the VS Code extension to the server URL (`aiTeamSync.serverUrl` in settings)
- Use the CLI with `export ATS_SERVER_URL=http://SERVER_IP:8400`

## Optional extras

- **Git hooks**: `./scripts/install-hooks.sh /path/to/repo` — auto-warns on commits to locked files
- **Slack/Telegram**: Edit `.env` with webhook URLs for push notifications
- **GitHub Action**: Auto-appends session context to PR descriptions

## License

MIT
