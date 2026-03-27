# MCP Server Setup for Claude Code

This enables Claude Code to natively use ai-team-sync for team coordination.

## Prerequisites

1. **ai-team-sync server running**:
   ```bash
   ats-server
   # Should be running on http://localhost:8400
   ```

2. **Install ai-team-sync with MCP support**:
   ```bash
   pip install -e .
   ```

---

## Claude Code Configuration

### Option 1: Auto-Configuration (Recommended)

Add to your Claude Code settings (`~/.claude/config.json`):

```json
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

### Option 2: Project-Specific Configuration

Copy `mcp-config.json` to your project root and Claude Code will auto-load it.

---

## Verify Installation

1. **Start Claude Code**
2. **Check available tools**:
   - Claude should list ai-team-sync tools
   - Look for: `start_session`, `check_locks`, `request_override`, etc.

3. **Test a tool**:
   ```
   You: Use start_session to lock src/**

   Claude: *calls start_session MCP tool*
   ✅ Session started!
   Session ID: abc123...
   Scope: src/**
   Locks created: 1
   ```

---

## Available MCP Tools

| Tool | Purpose | When to Use |
|------|---------|-------------|
| `start_session` | Start working session with locks | Beginning work on files |
| `check_locks` | Check if files are locked | Before editing files |
| `request_override` | Ask permission for locked files | When blocked by exclusive lock |
| `check_pending_requests` | See who's asking for overrides | Periodically during session |
| `respond_to_request` | Approve/deny override requests | When requests arrive |
| `team_status` | See what team is working on | Check before starting work |
| `complete_session` | End session and release locks | When done working |
| `log_decision` | Record design decisions | After making architecture choices |

---

## Usage Examples

### Example 1: Starting Work
```
You: I need to refactor the authentication system. Use ai-team-sync to announce this.

Claude: I'll start a session and lock the auth files.
*calls start_session with scope=["backend/auth/**"]*

✅ Session started!
Scope: backend/auth/**
Mode: Advisory
Team has been notified.

Let's begin the refactoring...
```

### Example 2: Checking for Conflicts
```
You: Edit backend/auth/middleware.py

Claude: Let me check if this file is locked first.
*calls check_locks with paths=["backend/auth/middleware.py"]*

🔒 Lock conflict detected:
⛔ backend/auth/middleware.py
   Locked by: alice
   Pattern: backend/auth/**
   Mode: exclusive

⛔ Exclusive lock blocks you. I'll request an override.
*calls request_override*

✅ Override request sent!
Owner: alice
Justification: Need to fix critical security vulnerability

Waiting for alice's approval...
```

### Example 3: Responding to Requests
```
Claude: *periodically calls check_pending_requests*

📬 1 pending override request:
From: bob
Pattern: backend/auth/**
Justification: Need to fix critical security vulnerability

You: Approve it, security is critical.

Claude: *calls respond_to_request with approved=true*

✅ APPROVED Override request!
Your message: Approved - security fixes take priority
Bob has been notified and can now proceed.
```

### Example 4: Completing Work
```
You: I'm done with the auth refactoring.

Claude: *calls complete_session*

✅ Session completed!
Summary: Refactored authentication to use JWT with refresh tokens
All locks released. Team has been notified.
```

---

## Auto-Detection (Future Enhancement)

Currently in development:

```python
# Future: Claude Code automatically detects file modifications
# and creates sessions without being asked

# You: *open backend/auth.py in editor*
# Claude: Detected work on backend/auth.py. Starting session...
```

---

## Troubleshooting

### MCP Server Not Loading

**Check 1**: Server is running
```bash
curl http://localhost:8400/health
# Should return: {"status":"ok","service":"ai-team-sync"}
```

**Check 2**: MCP binary is accessible
```bash
which ats-mcp
# Should show path like: /path/to/venv/bin/ats-mcp
```

**Check 3**: Claude Code config is valid
```bash
cat ~/.claude/config.json
# Verify JSON is valid
```

### Tools Not Showing Up

1. Restart Claude Code
2. Check Claude Code logs: `~/.claude/logs/mcp.log`
3. Verify MCP package installed: `pip show mcp`

### Connection Refused Errors

- Ensure `ats-server` is running: `ps aux | grep ats-server`
- Check correct port in config: `ATS_SERVER_URL`
- Firewall blocking localhost:8400?

---

## Security Notes

- MCP server runs in your user context
- Only local access (localhost:8400)
- Uses same auth as CLI tools
- No external network access required

---

## Performance

- MCP calls are async (non-blocking)
- Typical latency: <50ms per tool call
- Tools cache session ID (no repeated auth)
- Minimal overhead on Claude's operations

---

## Development & Debugging

Enable debug logging:

```json
{
  "mcpServers": {
    "ai-team-sync": {
      "command": "ats-mcp",
      "env": {
        "ATS_SERVER_URL": "http://localhost:8400",
        "ATS_DEBUG": "1"
      }
    }
  }
}
```

View MCP server logs:
```bash
tail -f ~/.claude/logs/ats-mcp.log
```

---

## Next Steps

1. ✅ Install and configure MCP server
2. ✅ Test with simple session
3. ⏭️ Set up team notifications (Slack/Telegram)
4. ⏭️ Install git hooks for pre-commit checking
5. ⏭️ Configure auto-approval policies (coming in v0.3.0)

---

## Resources

- **API Docs**: See `AGENT_COORDINATION.md` for full API reference
- **Examples**: See `examples/conflict-scenarios.md` for workflows
- **MCP Protocol**: https://modelcontextprotocol.io/
