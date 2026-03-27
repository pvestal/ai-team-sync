# Agent Integration Gaps & Required Changes

## Current State: Manual Coordination
The system works but requires **explicit API calls**. Real agents need automation.

---

## Critical Missing Features

### 1. **No Auto-Detection**
**Problem**: Agents don't auto-announce when they start working
```python
# Current: Manual
ats session start -s "src/**" -d "Working on models"

# Needed: Automatic
# Claude Code starts → system detects → session auto-created
```

**Solution**:
- MCP Server that Claude Code auto-loads
- VS Code extension for Cursor/Copilot
- Git hook triggers on first file modification

---

### 2. **Polling is Inefficient**
**Problem**: Agents must actively poll for override requests
```python
# Current: Agent loops checking for requests
while True:
    requests = get_pending_requests()
    if requests:
        handle_request(requests[0])
    time.sleep(5)  # Wastes cycles
```

**Solution**:
- **WebSocket/SSE** for real-time notifications
- **Webhooks** to agent endpoints
- **MCP subscription** (when agent supports it)

---

### 3. **No Decision Automation**
**Problem**: Approval logic is manual

**Current**:
```python
# Agent must implement custom logic
if "urgent" in request.justification:
    approve()
else:
    deny()
```

**Needed**:
- **Auto-approval policies** in config
- **LLM-based evaluation** of justifications
- **Time-based rules** (auto-approve after 5 min if no response)

Example config:
```toml
[approval_policies]
auto_approve_if = ["urgent", "security", "hotfix"]
auto_deny_if = ["breaking change", "refactor"]
llm_evaluate = true  # Use LLM to judge justification
timeout_action = "approve"  # What to do after 15min
```

---

### 4. **No MCP Integration**
**Problem**: Claude Code can't natively use ai-team-sync

**What's needed**:
```json
// MCP Server manifest
{
  "name": "ai-team-sync",
  "version": "0.2.0",
  "tools": [
    {
      "name": "start_session",
      "description": "Start a working session with scope locks",
      "parameters": {
        "scope": ["src/**"],
        "exclusive": false
      }
    },
    {
      "name": "check_locks",
      "description": "Check if files are locked before editing"
    },
    {
      "name": "request_override",
      "description": "Request permission to work on locked scope"
    }
  ]
}
```

Claude Code would automatically:
- Check locks before editing files
- Request overrides when blocked
- Respond to requests from other agents

---

### 5. **Session Lifecycle Unclear**
**Problem**: When does a session end?
- When agent finishes?
- When user closes chat?
- After N minutes of inactivity?

**Current**: Manual `ats session complete`

**Needed**:
- **Auto-complete on agent shutdown**
- **Inactivity timeout** (30 min no git activity)
- **Git hook**: Complete session on `git push`

---

### 6. **No IDE Integration**
**Problem**: Cursor, Copilot, Claude Code run in IDEs

**What users see**: Nothing. No conflicts until git merge.

**Needed**:
- **VS Code Extension** (status bar, notifications)
- **Cursor Plugin** (inline warnings)
- **JetBrains Plugin** (for PyCharm, IntelliJ users)

Example VS Code integration:
```typescript
// Shows in status bar: "🔒 3 active sessions"
// On file save: Checks locks, warns inline
vscode.window.showWarningMessage(
  "backend/auth.py locked by alice (exclusive). Request override?"
);
```

---

## Proposed Changes

### Phase 1: Make it Actually Automatic

#### A. MCP Server Implementation
```bash
# New package structure
src/
  ai_team_sync/
    mcp/
      server.py      # MCP server for Claude Code
      tools.py       # MCP tool definitions
      manifest.json  # MCP capabilities
```

**Auto-detection flow**:
1. Claude Code starts with MCP server enabled
2. MCP server detects git repo
3. Auto-creates session with scope from git status
4. Claude checks locks before every file edit

#### B. WebSocket Notifications
```python
# Real-time notifications instead of polling
@router.websocket("/ws/sessions/{session_id}")
async def session_websocket(websocket: WebSocket, session_id: str):
    await websocket.accept()
    while True:
        # Push notifications when:
        # - Override requests arrive
        # - Conflicts detected
        # - Session status changes
        event = await wait_for_event(session_id)
        await websocket.send_json(event)
```

#### C. Auto-Approval Policies
```python
# Server-side evaluation
class ApprovalPolicy:
    def evaluate(self, request: OverrideRequest) -> bool:
        # Check keywords
        if any(kw in request.justification.lower()
               for kw in ["urgent", "security", "hotfix"]):
            return True

        # Use LLM to evaluate
        if settings.llm_evaluate:
            prompt = f"Should I approve this request? {request.justification}"
            response = llm.complete(prompt)
            return "yes" in response.lower()

        return False
```

#### D. Session Lifecycle Automation
```python
# Auto-detect session end
async def monitor_session_activity(session_id: str):
    last_activity = datetime.now()
    while True:
        # Check git activity
        uncommitted = get_uncommitted_files()
        if uncommitted:
            last_activity = datetime.now()

        # Auto-complete after 30 min inactivity
        if datetime.now() - last_activity > timedelta(minutes=30):
            complete_session(session_id)
            break

        await asyncio.sleep(60)
```

---

## Comparison: With vs Without Automation

### Current (v0.2.0) - Manual
```python
# Agent A
ats session start -s "backend/**" --exclusive
# ... manually work ...
ats session complete

# Agent B (blocked)
ats session start -s "backend/auth/**"  # 409 error
# Agent must write code to handle this
request_id = create_override_request(...)
# Agent must poll
while True:
    status = check_status(request_id)
    if status == "approved": break
# Finally can work
```

**Problems**:
- 5+ manual steps
- Polling loop required
- No auto-detection
- Agent must handle all logic

---

### Proposed (v0.3.0) - Automated
```python
# Agent A (Claude Code with MCP)
# Opens file backend/models.py
# → MCP server auto-creates session
# → Auto-locks backend/**

# Agent B (Cursor with VS Code extension)
# Opens file backend/auth.py
# → VS Code shows: "⚠️ Locked by Claude Code (exclusive)"
# → Click "Request Override"
# → Enters justification: "Urgent security fix"
# → WebSocket notifies Agent A

# Agent A (automatic approval)
# → Policy evaluates: "urgent" → auto-approve
# → WebSocket notifies Agent B: "Approved"

# Agent B
# → VS Code shows: "✅ Access granted"
# → Can now edit backend/auth.py
```

**Benefits**:
- Zero manual steps
- No polling (WebSocket)
- Auto-detection via MCP/IDE
- Policy-based approval

---

## Bottom Line

### What Works Now (v0.2.0):
- ✅ API structure is solid
- ✅ Override request workflow is correct
- ✅ Database schema is complete
- ✅ Git integration exists

### What's Missing for Real Agents:
- ❌ Auto-detection (manual `ats session start`)
- ❌ Real-time notifications (polling only)
- ❌ Auto-approval policies (manual logic)
- ❌ MCP server (not exposed to Claude Code)
- ❌ IDE integrations (no VS Code/Cursor plugins)
- ❌ Session lifecycle automation (manual complete)

### Priority Changes for v0.3.0:
1. **MCP Server** - Make Claude Code natively aware
2. **WebSocket notifications** - Stop polling
3. **Auto-approval policies** - Let agents set rules
4. **Session auto-detection** - Hook into git/IDE
5. **VS Code extension** - Visual feedback

---

## Implementation Estimate

| Feature | Complexity | Time | Impact |
|---------|-----------|------|--------|
| MCP Server | Medium | 2-3 days | HIGH - Claude Code integration |
| WebSocket | Low | 1 day | HIGH - Real-time updates |
| Auto-policies | Medium | 2 days | HIGH - Reduces manual work |
| VS Code Extension | High | 5-7 days | MEDIUM - Better UX |
| Session auto-detect | Medium | 2 days | HIGH - Zero config |

**Total**: ~2 weeks for full agent automation
