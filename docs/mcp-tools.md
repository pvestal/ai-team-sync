# MCP tools

The complete tool surface ai-team-sync exposes to an MCP client (Claude Code,
Codex, or anything else that speaks stdio MCP). Generated against the live
registry; if this list and your client disagree, see
[Build identity and catalog staleness](#build-identity-and-catalog-staleness)
before concluding a tool is missing.

**Security note.** These tools call a local HTTP service that is unauthenticated
by design and expected to be loopback-bound. Anything a tool refuses is a
coordination guardrail — it stops a worker exceeding its role by accident and
makes roles declared and inspectable. It is not an access-control boundary, and
nothing here should be treated as one.

## Sessions

| Tool | Effect | Notes |
|---|---|---|
| `start_session` | mutates | Declares scope and takes advisory locks. Returns a context brief when one is available. Pass `repo_root` so patterns anchor to a repo. |
| `complete_session` | mutates | Releases locks. Refused while a child delegation is still open. |
| `pause_session` / `resume_session` | mutates | Pauses work while keeping locks. |
| `extend_scope` | mutates | Adds patterns to the current session mid-flight. |
| `get_session_details` | reads | Locks, decisions and commits for your session. |
| `team_status` | reads | Who is active, their scope, and staleness. |

Session-mutating tools act on *your* session, and the server resolves which one
that is from process-local identity. An identity that could have been written by
another agent is refused rather than guessed.

## Locks and presence

| Tool | Effect | Notes |
|---|---|---|
| `check_locks` | reads | Ask before editing. Anchored by repo so identical patterns in different repos do not collide. |
| `list_all_locks` | reads | Every active lock, with ids so a stale one can be reaped. |
| `whos_editing` | reads | Live presence: who has these files open right now. |
| `delete_lock` | mutates | Owner-bound. Refused while the holder is active and heartbeating; a lock left by a silent session stays reapable. |
| `pre_commit_check` | reads | Do staged files collide with someone's lock. |
| `check_git_changes` | reads | Uncommitted files inside your declared scope. |

## Decisions and overrides

| Tool | Effect | Notes |
|---|---|---|
| `log_decision` | mutates | Records what was chosen and why, for later readers. |
| `get_decision_history` | reads | Decisions from your session. |
| `request_override` | mutates | Ask the lock holder for permission to cross their claim. |
| `respond_to_request` | mutates | Owner-bound: only the session the request is addressed to may answer. |
| `check_pending_requests` / `check_my_override_requests` / `get_override_request_details` | reads | Inbox and status. |

## Delegation

See [Delegation](delegation.md) for the full contract.

| Tool | Effect | Notes |
|---|---|---|
| `delegate` | mutates | Hands a bounded subproblem to another worker. You keep the task. |
| `delegation_status` | reads | Whole lifecycle: ids, mode, state, the child's effective authority, lock counts. |
| `reconcile_delegation` | mutates | Owner-bound: only the parent accepts or rejects. |
| `my_authority` | reads | What you may do, base and effective. |

## Context and preflight

See [Context and preflight](context-and-preflight.md).

| Tool | Effect | Notes |
|---|---|---|
| `task_brief` | reads | Context packet for a piece of work, every line citation-bearing. |
| `preflight` | reads (records its own analysis) | Has this action already been tried, and what happened. Advisory. |

## Shared services

| Tool | Effect | Notes |
|---|---|---|
| `record_restart` | mutates | A restart is invisible to other sessions unless recorded. Deliberately session-optional. |
| `recent_restarts` | reads | Check before debugging something that "just broke". |

## Build identity and catalog staleness

| Tool | Effect | Notes |
|---|---|---|
| `ats_version` | reads | Commit and version of the MCP process *and* the REST service, and whether they agree. |

A stdio MCP server is spawned once per client session and holds its tool catalog
for that session's whole life. Redeploying ai-team-sync updates the REST service,
which restarts, and leaves every already-running client on its old catalog.

That means a client started before a deploy will not see tools added by it, and
the honest-looking conclusion — "this tool does not exist" — is wrong.

- Call `ats_version` first. It reports both revisions and says SKEW explicitly.
- On skew, restart the client session rather than debugging the server.
- Validate new tools from a **fresh** client, never from a long-running one.
- `scripts/check_mcp_parity.py` checks the installed entrypoint against the
  checkout and fails if they disagree.
