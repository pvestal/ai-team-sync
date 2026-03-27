# Agent-to-Agent Coordination Guide

ai-team-sync enables **autonomous AI agents** to coordinate work without human intervention.

## Problem: Agents Working in Parallel

When multiple AI agents (Claude Code, Cursor, Aider, etc.) work on the same codebase:
- They don't know what others are doing
- They create conflicting changes
- No record of WHY decisions were made

## Solution: Three-Tier Coordination

### 1. **Advisory Mode** (Default) - Parallel Work with Awareness
```python
# Agent A starts work
requests.post(f"{SERVER}/api/sessions", json={
    "developer": "agent-a",
    "agent": "claude-code",
    "scope": ["frontend/**"],
    "description": "Refactoring Vue components",
    "lock_mode": "advisory"  # default
})
# Response: 201 Created

# Agent B starts overlapping work (allowed)
requests.post(f"{SERVER}/api/sessions", json={
    "developer": "agent-b",
    "agent": "cursor",
    "scope": ["frontend/components/**"],
    "description": "Adding new login form",
    "lock_mode": "advisory"
})
# Response: 201 Created
# Notification sent to team channel: "Conflict detected"
```

**Result**: Both work proceeds. Humans resolve git conflicts later.

---

### 2. **Exclusive Mode** - Block Overlaps
```python
# Agent A starts critical work
requests.post(f"{SERVER}/api/sessions", json={
    "developer": "agent-a",
    "agent": "claude-code",
    "scope": ["backend/database/**"],
    "description": "Schema migration",
    "lock_mode": "exclusive"
})
# Response: 201 Created

# Agent B tries to overlap (blocked immediately)
requests.post(f"{SERVER}/api/sessions", json={
    "developer": "agent-b",
    "agent": "cursor",
    "scope": ["backend/database/migrations/**"],
    "lock_mode": "advisory"
})
# Response: 409 Conflict
# {
#   "error": "scope_conflict",
#   "message": "Cannot create session: scope 'backend/database/migrations/**'
#                conflicts with exclusive lock 'backend/database/**' held by agent-a"
# }
```

**Result**: Agent B is blocked. Must request override or wait.

---

### 3. **Override Request Workflow** - Agent-to-Agent Negotiation

When blocked by exclusive lock, Agent B can request permission:

```python
# Step 1: Agent B creates override request
response = requests.post(f"{SERVER}/api/override-requests", json={
    "requester_session_id": agent_b_session_id,
    "conflicting_pattern": "backend/database/**",
    "justification": "Need to add urgenthotfix for user authentication bug"
})
override_request_id = response.json()["id"]
# Notification sent to Agent A
```

```python
# Step 2: Agent A polls for pending requests
response = requests.get(f"{SERVER}/api/override-requests?session_id={agent_a_session_id}")
pending = [r for r in response.json() if r["status"] == "pending"]

if pending:
    request = pending[0]
    # Agent A evaluates: "Is agent-b's justification valid?"
    # Decision logic (could use LLM reasoning):
    if "urgent" in request["justification"].lower():
        approved = True
        message = "Approved - urgent fix takes priority"
    else:
        approved = False
        message = "Denied - schema migration in progress, wait 10 min"

    # Step 3: Agent A responds
    requests.post(f"{SERVER}/api/override-requests/{request['id']}/respond", json={
        "approved": approved,
        "message": message
    })
    # Notification sent back to Agent B
```

```python
# Step 4: Agent B polls for response
response = requests.get(f"{SERVER}/api/override-requests/{override_request_id}")
status = response.json()["status"]

if status == "approved":
    # Proceed with work despite conflict
    print(f"Override approved: {response.json()['response_message']}")
elif status == "denied":
    # Wait or work on different files
    print(f"Override denied: {response.json()['response_message']}")
elif status == "expired":
    # No response after 15 minutes, auto-expired
    print("Request expired - try again or coordinate directly")
```

---

## Git Integration for Agents

### Check Uncommitted Changes in Session Scope
```python
# Agent A wants to know what files it's actually modified
response = requests.get(f"{SERVER}/api/git/session/{session_id}/changes")
data = response.json()

print(f"Modified {data['total_files']} files:")
for pattern, files in data['files_by_pattern'].items():
    print(f"  Pattern '{pattern}': {files}")

# Example response:
# {
#   "session_id": "abc-123",
#   "scope_patterns": ["frontend/**"],
#   "uncommitted_files": ["frontend/App.vue", "frontend/components/Login.vue"],
#   "files_by_pattern": {
#     "frontend/**": ["frontend/App.vue", "frontend/components/Login.vue"]
#   },
#   "total_files": 2
# }
```

### Pre-Commit Lock Check
```python
# Before committing, agent checks for conflicts
response = requests.post(f"{SERVER}/api/git/pre-commit-check", json={
    "staged_files": None  # Auto-detect from git
})

data = response.json()
if not data["can_proceed"]:
    print("BLOCKED by exclusive locks:")
    for lock in data["blocking_locks"]:
        print(f"  {lock['file']} - locked by {lock['developer']}")
    # Agent decides: request override or unstage files

if data["advisory_locks"]:
    print("WARNING - overlapping advisory locks:")
    for lock in data["advisory_locks"]:
        print(f"  {lock['file']} - locked by {lock['developer']}")
    # Agent logs this but proceeds with commit
```

---

## Full Agent Workflow Example

```python
import requests
import time

SERVER = "http://localhost:8400"

class AIAgent:
    def __init__(self, name, agent_type):
        self.name = name
        self.agent_type = agent_type
        self.session_id = None

    def start_session(self, scope, description, exclusive=False):
        """Start a working session."""
        response = requests.post(f"{SERVER}/api/sessions", json={
            "developer": self.name,
            "agent": self.agent_type,
            "scope": scope,
            "description": description,
            "lock_mode": "exclusive" if exclusive else "advisory"
        })

        if response.status_code == 201:
            self.session_id = response.json()["id"]
            print(f"[{self.name}] Session started: {self.session_id[:8]}...")
            return True
        elif response.status_code == 409:
            # Conflict detected
            conflict = response.json()["detail"]
            print(f"[{self.name}] BLOCKED: {conflict['message']}")
            return False

    def request_override(self, pattern, reason):
        """Request permission to proceed despite conflict."""
        response = requests.post(f"{SERVER}/api/override-requests", json={
            "requester_session_id": self.session_id,
            "conflicting_pattern": pattern,
            "justification": reason
        })
        return response.json()["id"]

    def check_pending_requests(self):
        """Check for override requests from other agents."""
        response = requests.get(
            f"{SERVER}/api/override-requests?session_id={self.session_id}&status=pending"
        )
        return response.json()

    def respond_to_request(self, request_id, approved, message):
        """Approve or deny an override request."""
        requests.post(f"{SERVER}/api/override-requests/{request_id}/respond", json={
            "approved": approved,
            "message": message
        })

    def complete_session(self, summary):
        """Complete session and release locks."""
        requests.patch(f"{SERVER}/api/sessions/{self.session_id}", json={
            "status": "completed",
            "summary": summary
        })
        print(f"[{self.name}] Session completed")


# Example: Two agents coordinate autonomously
agent_a = AIAgent("agent-alice", "claude-code")
agent_b = AIAgent("agent-bob", "cursor")

# Agent A starts critical work
agent_a.start_session(["backend/**"], "Database migration", exclusive=True)

# Agent B tries to work on same area (gets blocked)
if not agent_b.start_session(["backend/auth/**"], "Add OAuth"):
    # Request override
    request_id = agent_b.request_override("backend/**", "Urgent security fix needed")
    print(f"[agent-bob] Override requested: {request_id[:8]}...")

    # Agent A checks requests (simulating polling)
    time.sleep(1)
    requests = agent_a.check_pending_requests()

    for req in requests:
        # Agent A evaluates justification (could use LLM)
        if "urgent" in req["justification"].lower():
            agent_a.respond_to_request(req["id"], approved=True, message="Approved - security takes priority")
        else:
            agent_a.respond_to_request(req["id"], approved=False, message="Wait for migration to complete")

    # Agent B checks response
    time.sleep(1)
    response = requests.get(f"{SERVER}/api/override-requests/{request_id}")
    if response.json()["status"] == "approved":
        print(f"[agent-bob] Approved! Proceeding with work...")
        # Now agent B can proceed despite the conflict
```

---

## Benefits for AI Teams

1. **No Human Bottleneck**: Agents negotiate autonomously
2. **Conflict Prevention**: Exclusive mode prevents parallel conflicting work
3. **Decision Logging**: All override requests/responses are persisted
4. **Graceful Degradation**: If server is down, work continues (no blocking)
5. **Git Integration**: Agents know exactly what files they've modified
6. **15-min Timeout**: Requests auto-expire to prevent deadlock

---

## API Summary

| Endpoint | Method | Purpose |
|----------|--------|---------|
| `/api/sessions` | POST | Start session with locks |
| `/api/sessions` | GET | List active team sessions |
| `/api/locks/check` | POST | Check if files are locked |
| `/api/override-requests` | POST | Request permission for conflict |
| `/api/override-requests` | GET | Poll for pending requests |
| `/api/override-requests/{id}/respond` | POST | Approve/deny request |
| `/api/git/session/{id}/changes` | GET | Get uncommitted changes |
| `/api/git/pre-commit-check` | POST | Check locks before commit |

All endpoints return JSON and use standard HTTP status codes.
