# Changelog

## [Unreleased]

### Fixed
- **Cross-agent session identity: one agent could redirect another's mutations**
  through the shared `~/.ats_session` pointer. Proven live 2026-09-11: a Codex
  parent delegated a READ_ONLY subtask; the delegated Claude's SessionStart wrote
  the shared pointer; the Codex parent has no CLAUDE_CODE_SESSION_ID and so no
  per-session pointer, resolved through the shared file, and its
  `complete_session` completed the CHILD's row — reporting "All locks released"
  while the parent stayed active holding its lock.
  Three changes. (1) `session_pointer.resolve_pointer_source()` returns WHERE an
  id came from, and `mcp.mutation_refusal()` refuses any session-mutating call
  whose identity resolved through the shared file, fails closed when identity is
  absent, and refuses an explicit binding that names another worker's row.
  (2) The MCP remembers the session THIS process started in memory, which is the
  one identity no other agent can write. (3) A delegated child is launched with
  its own `ATS_STATE_DIR` and with `ATS_SESSION_ID` set to the child row ATS
  already created, so it adopts that exact session and registers no second
  placeholder. Every hardcoded `~/.ats_session` path now resolves through
  `session_pointer`, so that isolation is real rather than nominal.
  The global pointer is still written and still read for back-compat; it is no
  longer accepted as proof of identity for a mutation. `record_restart` stays
  ungated deliberately: it creates a new row and is session-optional, and #2559
  has a test that a stale pointer must not lose the record.

### Added
- **Delegation as a first-class ATS object** (`delegation.py`, `models.Delegation`,
  `/api/delegations`, `ats delegate`, `delegate` MCP tool). One invariant:
  DELEGATION IS NOT HANDOFF. The parent keeps ownership for the whole life of the
  child, nothing in the child's lifecycle writes to the parent, and a parent with
  open children cannot be completed (409) — completing there would strand the
  child and leave the task owned by nobody.
  A mode is a SAFETY PROPERTY, not an audit label. READ_ONLY and VERIFY children
  cannot write files, commit, restart services, submit GPU work, mutate task
  state, or delegate onward; IMPLEMENT writes only inside its declared scope and
  still cannot close the parent's task. Enforced in three places: authority is
  the INTERSECTION of worker registry and mode (a mode can never grant what the
  worker lacks, or delegation becomes the escalation path around the registry),
  the server refuses a scope claim from a read-only child, and the launcher
  applies the harness's own restrictions (`--permission-mode plan`,
  `--disallowedTools`) so it is not honour-system.
  Recursive delegation is refused at depth 1: a chain of workers collaborating
  on one bug is a chain in which nobody owns it. Acceptance criteria are
  required, because without them the parent cannot reconcile what comes back.
  Leases expire, and an expired lease stops accepting evidence rather than
  taking work of unknown age.
  The child receives a freshly built packet (contract + prohibitions +
  brief-on-claim), never the parent's conversation — one worker's intermediate
  reasoning must not contaminate the next, and the exchange stays reproducible
  from the record alone.

### Added
- **Task-claim context packet** (`briefs.py`, `POST /api/brief`, `task_brief` MCP
  tool, and returned automatically by `start_session`). The claim is the trigger:
  a worker starts with live blockers, prior ATS decisions, prior work in its
  scope, and Echo Brain recall, instead of re-deriving them or re-running a lane
  already known to fail. Three rules the tests hold. Provenance is CARRIED, not
  flattened — OBSERVATION / INFERRED / VERIFIED / OPERATOR_DECISION, read from
  Echo's own `payload.trust` rather than guessed, and nothing is promoted here,
  because promotion needs an artifact. Every line carries a citation the reader
  can check (an `ats:decision/<id>`, a memory's file path); the first live run
  cited `echo:mem/qdrant/echo_memory` on every line, which is present, uniform
  and useless, so the digest fallback exists. Recall is best-effort: Echo Brain
  or ollama being down degrades the packet, never the claim.
  Ranking is a local embedding pass (`nomic-embed-text`, the resident model)
  across every section, because filtering decisions by repo is not relevance —
  before it, a two-body-contact objective surfaced an OAuth decision and a
  watchdog timer; after it, the top four are the contact canary that located the
  failure at identity binding. Compression is deterministic dedup, authority
  ordering and a character budget: a generative summariser would be a second
  place for a model to invent a fact, and would load a model the residency
  policy keeps evicted.

### Added
- **Worker capability + authority registry** (`workers.py`, `GET /api/workers`,
  `my_authority` MCP tool, example at `deploy/workers.toml`). A worker is a
  CLASS with declared capabilities (what it is good at, for routing) and
  declared authority (what it may do, enforced on claim) — two separate
  questions, because a local model can be capable of proposing a patch and have
  no authority to commit one. Labels resolve by stripping one ':'-segment at a
  time, so 'claude-code:fb0bb6bf' and 'local:qwen3-30b' both find their class.
  Enforced SERVER-side in `create_session`: a worker with edit authority 'none'
  is refused when it claims scope (403) and may register unscoped, and a worker
  class with a concurrency cap is refused a session beyond it (409). That
  placement is the point — the scope guard Claude Code runs is a PreToolUse
  hook, Codex has no hook mechanism and a local worker has no client, so a
  client-side rule would bind exactly one of the three. It is a guardrail, not
  access control: the API is unauthenticated by design, so this stops a worker
  exceeding its role by accident, not by intent.
  An UNREGISTERED label keeps pre-registry rights and is logged by name, so
  adding the registry cannot break a client that predates it (the VS Code
  extension and older CLI builds post agent="unknown"). `ATS_STRICT_WORKERS=1`
  drops unregistered workers to read-only once the fleet is registered.

### Fixed
- **Hooks were blind inside git worktrees**: both `pre_tool_use_lockcheck` and
  `post_tool_use_presence` found the repo root by walking up for a `.git`
  DIRECTORY. A linked worktree's `.git` is a FILE, so the walk sailed past the
  worktree root to the nearest ancestor that had one (on this box, the
  operator's `~/Documents`, itself a repo). Every edit from a worktree reported
  a path prefixed with the worktree's directory name and anchored to an
  unrelated repo, so `find_conflicts` saw no owner and the claim guard's
  coordinated-root gate never fired — the guards failed open for exactly the
  isolated-workspace flow the worktree skills encourage. Both now share
  `git_utils.resolve_repo_roots`, which returns the worktree root (paths stay
  repo-relative, one key per file in every checkout) and the SHARED repo root
  (locks and coordinated-repo gating bind across a project's worktrees).
  Submodules, whose `.git` file is also a pointer, anchor to themselves. A
  `.git` DIRECTORY now has to contain HEAD to count: `~/Documents/.git` on
  this box holds only `info/`, so git calls it "not a git repository" while
  an isdir() check captured every loose file underneath it.

### Added
- **Session liveness heartbeat (reaper Gap 1)**: nullable `Session.last_heartbeat`,
  `POST /api/sessions/{id}/heartbeat`, and a client `session_heartbeat.py` hook
  (wire as a per-turn `Stop` hook — tool-agnostic). A session that heartbeats and
  then goes silent for `session_heartbeat_timeout_minutes` (default 20) is reaped
  fast and its locks released, instead of a dead Claude process holding the lane.
  Sessions that never heartbeat keep the (now shorter) fallback window — never-worse.
- **Explicit startup cleanup sweep**: the server runs one lock/override/session sweep
  immediately on startup (`run_startup_cleanup`), so a restart promptly reclaims
  sessions/locks orphaned while it was down.
- **Override-inbox hook** (`override_inbox.py`, wire as `UserPromptSubmit`): injects
  pending override requests targeting your locks into the turn context, so the unlock
  handshake no longer needs polling or a human relay. Owner-only, fail-open.
- `last_heartbeat` is now surfaced on `SessionResponse` (diagnose phantom-active rows).
- Integration test for the `extend_scope` MCP tool (routes the MCP httpx client into
  the in-process ASGI app — the previously-missing harness).
- **Multi-agent identity**: `ATS_AGENT` env var explicitly sets the agent for any
  tool (e.g. `ATS_AGENT=codex`, `ATS_AGENT=ollama:<model>`); best-effort Codex
  auto-detection via `CODEX_*` env signature.
- **`ats decision list --all`**: read the whole team's decision log, not just the
  active session's.
- `AGENTS.md` contributor guide; tests for agent detection and decision listing.

### Fixed
- Checked-in service and setup paths now preserve the localhost-only ATS default
  instead of reintroducing `0.0.0.0` on reinstall.
- Stale agent/MCP gap docs now reflect the current Claude Code hook/MCP coverage
  and the remaining Codex/other-agent enforcement gap.
- **`pre_commit_check` MCP tool was a silent no-op**: it sent `{"paths": ...}` while
  the endpoint reads `staged_files`, and parsed `blocked`/`warned` while the endpoint
  returns `blocking_locks`/`advisory_locks`. The argument was dropped (server
  auto-detected staged files from its own cwd) and the response never matched, so it
  always reported "clear". Now wired correctly and tested end-to-end.
- **`whos_editing` was blind to a concurrent same-developer session**: presence was
  keyed by developer name (so two sessions of one git user clobbered each other) and
  exclusion was by developer. Presence is now keyed by `(developer, agent)`,
  `whos_editing` excludes by session via `exclude_agent`, and the presence hook emits
  a per-session agent label (from the PostToolUse `session_id`). A parallel session of
  the same person is now visible.

### Changed
- **Reaper fallback window** for non-heartbeating sessions cut from 12h to 4h
  (`session_inactivity_hours`), so dead lanes don't sit parked all day.
- **Security:** `ATS_HOST` now defaults to `127.0.0.1` (was `0.0.0.0`); the write
  API is unauthenticated and should not bind all interfaces by default.
- `uvicorn` no longer runs with `reload=True` in production (`ats-server`).

### Removed
- Dropped a bundled, environment-specific `vision-qa` MCP plugin from the public
  package (it belonged to a private deployment).

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
