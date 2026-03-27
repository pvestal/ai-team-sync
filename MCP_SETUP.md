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

## Available MCP Tools (18 Total)

### Core Session Management
| Tool | Purpose | When to Use |
|------|---------|-------------|
| `start_session` | Start working session with locks | Beginning work on files |
| `pause_session` | Pause session (keep locks) | Switching tasks temporarily |
| `resume_session` | Resume paused session | Returning to paused work |
| `get_session_details` | View current session info | Check session status |
| `complete_session` | End session and release locks | When done working |

### Lock Coordination
| Tool | Purpose | When to Use |
|------|---------|-------------|
| `check_locks` | Check if files are locked | Before editing files |
| `list_all_locks` | See all active locks | Get team overview |
| `delete_lock` | Remove specific lock | Manual lock management |

### Override Requests
| Tool | Purpose | When to Use |
|------|---------|-------------|
| `request_override` | Ask permission for locked files | When blocked by exclusive lock |
| `check_pending_requests` | See requests TO you | Periodically during session |
| `check_my_override_requests` | See requests FROM you | Monitor your request status |
| `respond_to_request` | Approve/deny requests | When requests arrive |
| `get_override_request_details` | Get request details | Check specific request |

### Team Awareness
| Tool | Purpose | When to Use |
|------|---------|-------------|
| `team_status` | See active sessions | Check before starting work |
| `log_decision` | Record design decisions | After making architecture choices |
| `get_decision_history` | View session decisions | Review what was decided |

### Git Integration
| Tool | Purpose | When to Use |
|------|---------|-------------|
| `check_git_changes` | See uncommitted files | Before committing |
| `pre_commit_check` | Validate files for locks | Before git commit |

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

### Example 5: Auto-Approval (NEW!)
```
You: Request override for backend/** - this is an urgent security fix

Claude: *calls request_override with justification containing "urgent"*

✅ Override request AUTO-APPROVED!
🤖 Reason: Auto-approved based on justification keywords

Request ID: xyz789...
Pattern: backend/**
Owner: alice

You can now proceed with your changes immediately!
```

**Auto-approval keywords**: urgent, security, hotfix, critical
**Auto-deny keywords**: breaking change, major refactor

### Example 6: Session Pause/Resume (NEW!)
```
You: Need to switch branches temporarily

Claude: *calls pause_session*

⏸️ Session paused!
Locks: 3 (retained)
Use resume_session to continue work.

[... work on other branch ...]

You: Resume my previous session

Claude: *calls resume_session*

▶️ Session resumed!
Scope: backend/auth/**
Locks: 3
Ready to continue where you left off.
```

### Example 7: Checking Your Changes (NEW!)
```
You: What files have I changed?

Claude: *calls check_git_changes*

📝 8 uncommitted file(s) in your scope:

  modified: backend/auth/middleware.py
  modified: backend/auth/tokens.py
  new: backend/auth/refresh.py
  ...

✅ All changes are within your declared scope.
```

---

## Enhanced Features

### ✅ Session Persistence
Sessions survive MCP server restarts - stored in `~/.ats_session`

### ✅ Auto-Approval Policies
Configure in `.ai-team-sync.toml`:
```toml
[approval]
auto_approve_keywords = ["urgent", "security", "hotfix", "critical"]
auto_deny_keywords = ["breaking change", "major refactor"]
```

### ✅ Real-Time WebSocket Notifications
Server broadcasts lock expirations and override responses in real-time

### ✅ Conflict Resolution Guidance
When conflicts occur, Claude receives contextual suggestions:
- Request override with auto-approval keywords
- Coordinate with lock owner
- Adjust scope to avoid overlap

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
3. ✅ Configure auto-approval policies
4. ✅ Install git hooks for pre-commit checking
5. ⏭️ Set up team notifications (Slack/Telegram)
6. ⏭️ Set up WebSocket monitoring for real-time updates

---

## Resources

- **API Docs**: See `AGENT_COORDINATION.md` for full API reference
- **Examples**: See `examples/conflict-scenarios.md` for workflows
- **MCP Protocol**: https://modelcontextprotocol.io/
