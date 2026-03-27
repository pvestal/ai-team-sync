# ai-team-sync

Change management toolkit for teams working with AI coding agents (Claude Code, Cursor, Copilot Workspace, etc.).

When multiple developers each work with AI agents, changes happen faster and with larger blast radius than traditional development. **ai-team-sync** gives your team lightweight coordination: scope announcements, conflict prevention, decision logging, and real-time notifications.

## The Problem

- Developer A tells Claude Code to refactor the auth module
- Developer B tells Cursor to update the auth middleware
- Neither knows about the other until they both open PRs with conflicting changes
- Nobody recorded *why* the AI chose approach X over Y

## What This Does

```
Developer Terminal              Team Visibility
      |                              |
   ats CLI ────> FastAPI API ───> Slack / Telegram / VS Code
      |               |
  git hooks       SQLite/Postgres
      |               |
  GitHub Action    Sessions, Locks,
      |            Decisions
  PR enrichment
```

**Sessions** — Declare what you're working on before you start. Your team sees it instantly.

**Scope Locks** — Advisory or exclusive locks on file patterns. Get warned (or blocked) before stepping on someone's work.

**Decision Logs** — Record why your AI chose approach X over Y. Persists beyond the chat session.

**Notifications** — Slack, Telegram, and VS Code toast notifications when teammates start/finish work or hit conflicts.

**PR Enrichment** — GitHub Action that appends session context and decisions to PR descriptions automatically.

## Quick Start

### Install

```bash
pip install -e ".[dev]"
```

### Start the server

```bash
cp .env.example .env
# Edit .env with your Slack/Telegram tokens (optional)
ats-server
```

### Use the CLI

```bash
# Start a session — team gets notified
ats session start -s "src/auth/**" -d "Refactoring auth to use JWT"

# Check if a file is locked by someone else
ats lock check src/auth/middleware.py

# Log a design decision
ats decision log "Chose JWT over sessions" \
  -c "JWT" -r "session cookies" \
  --reason "Stateless auth needed for horizontal scaling"

# See what the team is working on
ats team

# Done — release locks, notify team
ats session complete -m "Auth now uses JWT, added refresh token rotation"
```

### Install git hooks (optional)

```bash
./scripts/install-hooks.sh /path/to/your/repo
```

This installs:
- **pre-commit**: Warns (or blocks) if you're committing to locked files
- **post-commit**: Auto-logs commits to your active session
- **prepare-commit-msg**: Appends session ID to commit messages for PR enrichment

## VS Code Extension

The `vscode-extension/` directory contains a VS Code extension that:
- Shows a status bar item with active session count
- Pops **toast notifications** when teammates start/complete sessions
- Warns when you save a file that's locked by someone else
- Provides commands: Start Session, Complete Session, Check Locks, Show Team Status

### Install the extension

```bash
cd vscode-extension
npm install
npm run compile
npm run package
code --install-extension ai-team-sync-0.1.0.vsix
```

### Configure

In VS Code settings:
- `aiTeamSync.serverUrl`: Server URL (default: `http://localhost:8400`)
- `aiTeamSync.pollIntervalSeconds`: Poll interval (default: 15s)
- `aiTeamSync.showLockWarnings`: Warn on saving locked files (default: true)

## Configuration

### Team config (`.ai-team-sync.toml` — check into repo)

```toml
[server]
url = "http://your-server:8400"

[locks]
default_mode = "advisory"   # advisory (warn) or exclusive (block)
ttl_hours = 8               # auto-expire forgotten locks

[notifications]
events = ["session.started", "session.completed", "lock.conflict"]
```

### Environment (`.env` — do NOT check in)

```bash
DATABASE_URL=sqlite+aiosqlite:///ai_team_sync.db
SLACK_WEBHOOK_URL=https://hooks.slack.com/services/...
TELEGRAM_BOT_TOKEN=123456:ABC-DEF...
TELEGRAM_CHAT_ID=-100123456789
```

## GitHub Action

Add the PR enrichment workflow (`.github/workflows/pr-enrichment.yml`) to your repo. It:
1. Scans commit messages for session IDs (added by the prepare-commit-msg hook)
2. Fetches session details and decisions from the API
3. Appends an "AI Session Context" section to the PR body

Set `ATS_SERVER_URL` as a repository variable pointing to your server.

## API Endpoints

| Method | Path | Description |
|--------|------|-------------|
| `GET` | `/health` | Health check |
| `POST` | `/api/sessions` | Create a session |
| `GET` | `/api/sessions` | List sessions (filter: `?status=active`) |
| `GET` | `/api/sessions/{id}` | Get session details |
| `PATCH` | `/api/sessions/{id}` | Update/complete a session |
| `POST` | `/api/locks` | Create a scope lock |
| `GET` | `/api/locks` | List active locks |
| `POST` | `/api/locks/check` | Check paths against locks |
| `DELETE` | `/api/locks/{id}` | Delete a lock |
| `POST` | `/api/decisions` | Log a decision |
| `GET` | `/api/decisions` | List decisions (filter: `?session_id=...`) |
| `GET` | `/api/decisions/{id}` | Get decision details |

## Architecture

- **Python 3.11+** with FastAPI and async SQLAlchemy
- **SQLite** by default (zero infrastructure), **PostgreSQL** for shared teams
- **Click** CLI with git integration
- **Notification adapters** for Slack (webhook) and Telegram (Bot API)
- **VS Code extension** (TypeScript) with polling + toast notifications
- **GitHub Action** for PR enrichment

## Development

```bash
# Install with dev dependencies
pip install -e ".[dev]"

# Run tests
pytest

# Format
black src/ tests/

# Type check
mypy src/
```

## License

MIT
