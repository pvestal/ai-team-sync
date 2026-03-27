# MCP Tool Gaps Analysis

## Current MCP Tools (8 tools)

1. ✅ `start_session` - POST /api/sessions
2. ✅ `check_locks` - POST /api/locks/check
3. ✅ `request_override` - POST /api/override-requests
4. ✅ `check_pending_requests` - GET /api/override-requests?session_id={id}&status=pending
5. ✅ `respond_to_request` - POST /api/override-requests/{id}/respond
6. ✅ `team_status` - GET /api/sessions?status=active
7. ✅ `complete_session` - PATCH /api/sessions/{id}
8. ✅ `log_decision` - POST /api/decisions

## Missing MCP Tools (API endpoints not exposed)

### High Priority - Essential for agents

1. **`pause_session`** - PATCH /api/sessions/{id} with status=paused
   - **Gap**: Agents can only complete sessions, not pause/resume
   - **Use case**: Agent needs to switch branches or pause work temporarily
   - **Impact**: Forces agents to complete sessions unnecessarily

2. **`resume_session`** - PATCH /api/sessions/{id} with status=active
   - **Gap**: No way to resume paused sessions
   - **Use case**: Resume work after pause
   - **Impact**: Have to create new session instead of resuming

3. **`get_session_details`** - GET /api/sessions/{id}
   - **Gap**: Can't get details about current session
   - **Use case**: Check session status, locks, decisions, commits
   - **Impact**: No visibility into own session state

4. **`check_git_changes`** - GET /api/git-status/session/{id}/changes
   - **Gap**: Can't see uncommitted files in session scope
   - **Use case**: Before commit, see what files changed in scope
   - **Impact**: No pre-commit validation of scope

5. **`check_my_override_requests`** - GET /api/override-requests?session_id={id}&status=pending (as requester)
   - **Gap**: Can see requests TO them, but not requests FROM them
   - **Use case**: Monitor status of override requests they made
   - **Impact**: No visibility into request status (approved/denied/pending)

### Medium Priority - Quality of life

6. **`list_all_locks`** - GET /api/locks
   - **Gap**: Can only check specific paths, not see all locks
   - **Use case**: See overall team lock landscape
   - **Impact**: Limited visibility into team coordination

7. **`get_decision_history`** - GET /api/decisions?session_id={id}
   - **Gap**: Can log decisions but can't retrieve them
   - **Use case**: Review decisions made during session
   - **Impact**: Decisions are write-only for agents

8. **`get_override_request_details`** - GET /api/override-requests/{id}
   - **Gap**: Can't get details of specific override request
   - **Use case**: Check status of a request by ID
   - **Impact**: Have to poll pending list

9. **`delete_lock`** - DELETE /api/locks/{id}
   - **Gap**: Can't manually remove individual locks
   - **Use case**: Release specific lock without completing session
   - **Impact**: All-or-nothing lock management

10. **`pre_commit_check`** - POST /api/git-status/pre-commit-check
    - **Gap**: Git hook functionality not exposed to MCP
    - **Use case**: Validate commit before attempting
    - **Impact**: Agents can't use pre-commit validation

## Functional Gaps (not API-related)

### 11. **Session Persistence**
- **Gap**: `_active_session_id` is in-memory only
- **Impact**: Session lost on MCP server restart
- **Solution**: Store in `~/.ats_session` file like CLI does

### 12. **WebSocket Integration**
- **Gap**: MCP tools don't use WebSocket for real-time updates
- **Impact**: Agents must poll for override responses
- **Solution**: Add WebSocket client to MCP server for event notifications

### 13. **Auto-approval Feedback**
- **Gap**: When request is auto-approved/denied, agent doesn't know why
- **Impact**: Agent sees "approved" but doesn't know it was automatic
- **Solution**: Include auto-approval info in response message

### 14. **Bulk Lock Checking**
- **Gap**: Have to check locks individually
- **Impact**: Inefficient for agents editing many files
- **Solution**: Already supported via check_locks array, but could batch better

### 15. **Session Auto-start**
- **Gap**: Agents must manually call start_session
- **Impact**: Easy to forget, leads to uncoordinated work
- **Solution**: Hook into Edit/Write tool usage to auto-start

### 16. **Conflict Resolution Guidance**
- **Gap**: When conflicts detected, no suggestions provided
- **Impact**: Agents don't know what to do next
- **Solution**: Include suggested actions in conflict responses

### 17. **Lock Expiration Handling**
- **Gap**: No notification when locks expire
- **Impact**: Session continues but locks are gone
- **Solution**: WebSocket event for lock expiration

### 18. **Multi-session Support**
- **Gap**: Only one active session per MCP server instance
- **Impact**: Can't work on multiple branches simultaneously
- **Solution**: Support multiple session IDs (per-branch tracking)

## Recommended Implementation Order

### Phase 1 (Critical - Blocks agent usage)
1. Session persistence (~/.ats_session)
2. `pause_session` / `resume_session`
3. `get_session_details`
4. `check_my_override_requests` (requester view)

### Phase 2 (High value - Improves UX)
5. WebSocket integration for real-time notifications
6. `check_git_changes`
7. `list_all_locks`
8. Auto-approval feedback in responses

### Phase 3 (Nice to have)
9. `get_decision_history`
10. `pre_commit_check`
11. Multi-session support
12. Conflict resolution guidance

### Phase 4 (Polish)
13. `delete_lock`
14. `get_override_request_details`
15. Lock expiration notifications
16. Session auto-start on first file edit

## Comparison with Other AI Coding Tools

### Cursor
- **Lacks**: Any team coordination
- **ai-team-sync advantage**: All team coordination features

### GitHub Copilot Workspace
- **Has**: PR creation, issue tracking
- **Lacks**: Real-time lock coordination
- **ai-team-sync advantage**: Prevents conflicts before they happen

### Replit Agent
- **Has**: Multiplayer editing
- **Lacks**: Structured decision logging, approval workflows
- **ai-team-sync advantage**: Policy-based coordination, audit trail

## Integration Opportunities

### 1. Claude Code Hooks
- Hook into Read/Edit/Write tools to auto-check locks
- Hook into git commits to auto-log changes
- Hook into session start/end for lifecycle management

### 2. Cursor Integration
- Cursor could use ats-server via REST API
- Cursor multiplayer + ats = enterprise-grade coordination

### 3. VS Code Extension
- Status bar showing active session
- Lock warnings in editor gutters
- Override request notifications

### 4. GitHub Actions
- CI/CD pipeline checks for active locks
- Auto-complete sessions on PR merge
- Decision summary in PR descriptions

## Summary

**Total Gaps Identified**: 18
- **API endpoints not exposed**: 10
- **Functional gaps**: 8

**Most Critical** (implement first):
1. Session persistence
2. Pause/resume session
3. Get session details
4. Check requester override requests
5. WebSocket real-time notifications
