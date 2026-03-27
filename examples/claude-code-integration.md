# Using ai-team-sync with Claude Code

## Setup

1. Install ai-team-sync: `pip install ai-team-sync`
2. Start the server: `ats-server`
3. Install git hooks in your repo: `./scripts/install-hooks.sh .`

## Workflow

### Before starting a Claude Code session

```bash
# Announce your scope to the team
ats session start -s "src/auth/**" -s "migrations/" -d "Refactoring auth to JWT with Claude"
```

Your teammates see a notification in Slack/Telegram/VS Code:
> Patrick started working on `src/auth/**, migrations/` with claude-code

### During the session

Claude Code works normally. The git hooks handle the rest:
- **pre-commit** checks if you're stepping on someone's locks
- **post-commit** logs each commit to your session
- **prepare-commit-msg** tags commits with your session ID

Log important decisions:
```bash
ats decision log "Chose JWT over session cookies" \
  -c "JWT with refresh tokens" \
  -r "Express sessions with Redis" \
  --reason "Need stateless auth for k8s horizontal scaling" \
  -f src/auth/jwt.py -f src/auth/middleware.py
```

### After the session

```bash
ats session complete -m "Auth fully migrated to JWT. Added refresh token rotation and blacklisting."
```

Locks are released. Team gets notified. When you open a PR, the GitHub Action appends all session context and decisions to the PR body.

## Claude Code Hooks Integration

You can add ai-team-sync to your Claude Code hooks in `.claude/settings.json`:

```json
{
  "hooks": {
    "preToolUse": [
      {
        "matcher": "Edit|Write",
        "command": "ats lock check $(echo '$INPUT' | jq -r '.file_path // empty' 2>/dev/null)"
      }
    ]
  }
}
```

This checks locks before Claude Code edits any file.

## Tips

- Start sessions with specific scope patterns, not broad ones like `**/*`
- Use `ats team` to check what others are working on before starting
- Log decisions for non-obvious choices — future you (and your teammates) will thank you
- The session summary should answer "what changed and why" — this ends up in PRs
