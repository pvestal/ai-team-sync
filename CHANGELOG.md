# Changelog

## [Unreleased]

### Fixed
- **Lock readers compare where a path actually is (#2761).** `POST /api/locks/check`
  and `POST /api/git/pre-commit-check` matched the raw path against the raw
  repo-relative pattern and skipped locks by a string compare of roots, so an
  absolute path in a repository was never covered by that repository's own
  exclusive lock and pre-commit returned `can_proceed: true` over it. Both readers
  now place each path and each lock once per request, purely lexically (no
  filesystem I/O), and compare with `fnmatch`: anchored locks are compared in
  absolute space, unanchored legacy locks still match in every repository (and are
  never hidden by a caller `repo_root`), a rootless relative caller keeps the legacy
  conservative answer, and input that cannot be placed (NUL, a non-absolute root,
  `..` escapes) falls back to the pre-#2761 rule instead of raising. Placement
  only adds coverage: a lock the pre-#2761 rule reported is still reported unless
  it is an anchored lock of another repository, and a directory-form query
  (`src/`, `src/.`) keeps its trailing `/`, so `src/**` still covers it. A
  generated base-vs-candidate differential test pins both. When several
  locks cover a path, `locks/check` reports an exclusive one. MCP `whos_editing` now
  accepts and forwards `repo_root`. Contract and truth table: `docs/lock-readers.md`.
- **A lock is refused where a session would be (#2756, Gap 4).** `POST /api/locks`
  inserted unconditionally, so a session could lay an exclusive lock over another
  live session's claim that `start_session` would have refused, and
  `extend_scope`'s refusal branch was dead code. `create_lock` now applies session
  creation's own overlap check and refusal rule — one rule, shared, excluding the
  session's own locks — and answers 409 `scope_conflict` naming the holder.
  Repo anchoring and advisory sharing are unchanged. `extend_scope` now takes
  locks before declaring scope, adds only granted patterns, and reports
  `NOT extended` / `PARTIALLY extended` with each refusal instead of "Scope
  extended". The #2741 grant tests that relied on the old unconditional insert
  now write the foreign lock row directly; their grant assertions are unchanged.

### Added
- **The worker ATS names is the worker that actually ran.** `delegate` used one
  hardcoded command line, so `--worker codex` created a row labelled
  `codex:delegate`, applied Codex's authority envelope, and then ran the CLAUDE
  binary. Proven live on two delegations by process tree while `/usr/bin/codex`
  sat installed and unused; both closed as satisfied Codex work. Replaced by a
  per-worker launch specification owning the binary, the prompt convention AND
  the per-mode enforcement flags — swapping only the binary would have handed
  Claude's `--disallowedTools` to Codex, where they mean nothing, silently
  decaying READ_ONLY into a promise. A worker/mode pair with no enforcement
  mapping fails closed *before* anything is spawned, so a refused routing leaves
  no delegation row and no orphan child session. The absolute executable is
  resolved in the parent and stored as `delegations.resolved_binary` alongside
  `requested_worker`; the server re-derives the pairing from the registry, so
  requested=codex + resolved=claude is a 409 that stores nothing. Identity is
  deliberately NOT taken from the child's self-report: `child_env` injects
  `ATS_AGENT`, so that would compare the parent's own text with itself.
- **A delegated worker inherits the task's authority, not just history.** When a
  delegation names a Tower task, the packet now carries that task's canonical
  envelope — id, key, project, status, gate, claim, operator ruling,
  `verified_by`, and the full description holding the acceptance criteria and
  prohibited approaches — rendered *before* the objective, because a worker that
  reads the objective first starts solving. It stays a separate block from the
  context brief: the envelope is current binding authority, the brief is prior
  history with per-line provenance. A closed task renders an explicit
  already-closed warning with its closure evidence. A task named explicitly
  whose envelope cannot be fetched refuses the delegation before any record
  exists. Every worker receives a byte-identical packet, so authority is
  worker-independent by construction.
- **Preflight answers about the live task, not only its history.** With
  `task_id`, the Tower task is read before any evidence is gathered. A finished
  task returns the new `ALREADY_COMPLETED` disposition carrying `verified_by`; a
  live claim or a blocking gate returns `CAUTION` naming the holder or the gate;
  an unknown id fails closed persisting nothing. Live state outranks recall when
  they disagree, and the conflict is shown rather than silently resolved.
- **`complete_session` completes the session you name.** It accepts an
  authoritative `session_id` and returns the row, agent, prior→resulting status,
  timestamp and how the target was chosen. Existence and ownership settle before
  any pointer question, so a missing session is refused as missing and another
  worker's as theirs. Only a pointer that speaks for *this* caller can refuse a
  mismatch; the shared `~/.ats_session` never can, which is what lets a
  delegated child complete itself while the global file still names its parent.
  Omitting the id is the legacy path, unchanged and still refusing the shared
  pointer, but now reporting the resolved id. A terminal session is reported,
  not re-patched, because the endpoint is not idempotent.
- **`scripts/proof_context.sh`** — what code a live proof actually exercises:
  both repo HEADs, the deployed ATS commit with pid and start time, the
  echo-brain process against its last commit, and every live `ats-mcp` with its
  spawn time. Written after two proofs ran against code that was not the code
  under test: a bare `pipx install --force` skipped the build stamp, and a stdio
  MCP server kept its spawn-time build so a session closure exercised the old
  handler after the new one was deployed.

### Changed
- **VERIFY is verification-capable, non-authoritative, non-implementation.**
  Operator ruling 2026-09-12, second coordination tranche. `d685142` repaired
  READ_ONLY and deliberately left VERIFY on plan mode as a named gap. Measuring it
  found the gap was worse than recorded: plan mode refused every ATS and Echo Brain
  call as expected, AND across **three** independent runs the child declined the
  shell writes *itself* rather than the harness denying them — it even declined to
  run the unmodified CI command `python3 -m pytest -q`, because pytest writes
  `__pycache__`. So the mode whose purpose is to RUN verification could not run it,
  and the filesystem guarantee it appeared to give was the model's own compliance.
  Plan mode also left `Task`/`Agent` available, so the subagent escape was open.
  VERIFY now gets a real shell, and containment is environmental because a shell
  can write files and no tool list can honestly say otherwise. Two existing
  mechanisms, neither invented here: Claude Code's own Bash sandbox (bubblewrap +
  seccomp) declared with `failIfUnavailable` so a host without a backend REFUSES
  rather than running unconfined and `allowUnsandboxedCommands: false` so the
  per-call escape hatch is rejected; plus a disposable linked git worktree as the
  writable working directory. Measured: a write aimed at the lead tree fails at the
  kernel with `Read-only file system`. VERIFY's coordination reads and denial list
  are IDENTICAL to READ_ONLY's — one answer to "what may a delegated child read",
  asserted as set algebra so the only difference is `Bash`.
  The sandbox's writable set is the working directory plus the system temp root,
  and that edge is not hypothetical: the first containment measurement put the
  fixture lead repo in the temp root alongside the worktree and the child wrote
  into it with exit 0. The supervisor now REFUSES to launch when the lead
  repository is inside the temp root, rather than launching something contained
  only in appearance.
  READ_ONLY is unchanged, pinned as the exact token sequence `d685142` shipped.
  Codex READ_ONLY and Codex VERIFY are untouched: their containment is Codex's own
  `--sandbox read-only` runtime, so they get no Claude settings and no worktree.
- **A VERIFY child reviews the lead's ACTUAL result, and can prove it.** The lead's
  result is usually still uncommitted when review happens, so `git worktree add
  <rev>` alone would reproduce the commit and silently drop the work under review.
  The supervisor replays the tracked diff, copies untracked files, and writes a
  manifest carrying the base commit, the diff hash and a per-file sha256 comparison
  against the lead — placed in the child's packet ahead of the objective, so the
  child can check the claim instead of trusting it. A diff that does not apply
  cleanly is a refusal, because a partially reproduced result makes an invalid
  review look valid. The worktree is DETACHED, so a commit in it advances no
  branch, and teardown prunes it to unreachable; `git push` needs a network the
  sandbox denies. Teardown joins session finalization as the supervisor's duty on
  every terminal outcome, including a refused environment and a spawn that never
  ran.
- **READ_ONLY bounds the work product, not the coordination plane.** Operator
  ruling 2026-09-12, after a Codex-led canary. Claude READ_ONLY was
  `--permission-mode plan`, which refuses EVERY MCP call including pure reads —
  measured verbatim: `Cannot call mcp__ai-team-sync__my_authority while in plan
  mode.` The child launched as the right binary, received the full Tower Task
  envelope, did useful analysis, and then could not read its own authority, its
  own session, its delegation state, or the decision history it was delegated to
  consult. So the implementation equated "cannot mutate the repository" with
  "cannot participate in coordination", and the delegated lifecycle could not
  complete. Plan mode was also never a no-write guarantee: it writes a plan file
  under `~/.claude/plans/`.
  Replaced with a capability split: `--tools Read,Grep,Glob` (an allow-list of
  BUILT-INS, so an unlisted one is absent rather than refused — and absent "in
  subagents as well as here", which closes the spawn-a-subagent-to-write escape),
  `--permission-prompts none` (anything unlisted is denied automatically, so the
  policy is fail-closed rather than dependent on nobody answering a prompt), a
  per-tool MCP allow-list for the six ATS reads plus Echo Brain's canonical
  `get_tower_task`, and an explicit deny-list as a second lock and a readable
  contract. Bash is gone by design: it is the single path to `git commit`, `git
  push` and `echo > file`, and a command-prefix allow-list is pattern matching on
  a composable shell rather than a boundary.
  Mutability was determined by INSPECTING each handler, never by reading a tool
  name, and the names mislead in both directions: `check_locks` and `whos_editing`
  are POSTs whose handlers issue no write at all, while `delegate` issues no HTTP
  whatsoever and shells out to `ats delegate` — recursive delegation, which
  READ_ONLY prohibits. A name-based or verb-based allow-list would have let that
  one through.
  `VERIFY` deliberately keeps plan mode and therefore keeps the same coordination
  limitation: it is the one mode that must run tests, so it needs a shell. Named
  as a known remaining gap rather than silently widened. Codex enforcement is
  untouched — its own `--sandbox read-only` runtime, not harness flags.
- **The supervisor finalizes the child session, unconditionally.** The launcher
  already closed the exact child on clean exit, non-zero exit and lease expiry, so
  the preferred architecture was already the observed behaviour — but it was not
  deterministic. Two orphan paths are now closed: `subprocess.run` raising
  anything other than `TimeoutExpired` (a spawn that never started, an OSError, a
  signal) propagated and skipped finalization entirely, leaving the child session
  ACTIVE with its delegation open; and the result POST and the session close were
  sequential in one unguarded block, so a refused or unreachable `/return` left
  the session open. A child that failed to START cannot clean up after itself.
  Child self-close is removed from the READ_ONLY acceptance requirements rather
  than re-enabled: `complete_session` is a coordination mutation the launch spec
  denies, and delegation correctness must not depend on a model remembering to
  call it. The child packet never asked for it, and a test now holds that. The
  parent session is untouched on every terminal outcome.
- **Frontier close authority is model-neutral: `codex` joins `claude-code` in the
  conditional `task_close` class.** Operator ruling 2026-09-12. The Codex-led
  lead-worker canary stopped correctly before selecting work, because the
  deployed registry gave Claude `conditional` and Codex `no` — so the lifecycle
  under test was unprovable by anyone but Claude, which is a property of the
  client rather than of the work. One registry value changed; there is
  deliberately NO Codex-specific closure policy, because the conditions
  (ownership, Tower Task envelope, acceptance criteria, blocking gates,
  verification, closure evidence, CI) live on the shared conditional path both
  workers already read. Conditional is still not a yes: `may_close_task` remains
  the unconditional question and answers NO for both, and the acceptance evidence
  decides at the gate. Unchanged by design — `local`, `default` and `restricted`
  stay at `no`; no worker is unconditional; and no delegation mode grants closing
  to a child, since `effective_authority` is a rank-keyed intersection and every
  mode envelope caps `task_close` at `no`, so a READ_ONLY or VERIFY child of a
  conditional parent still closes nothing. A future worker such as Cursor
  resolves to `default` and gets no close authority by arriving.
  The pre-ruling Codex test could not have caught this either way: it asserted
  only `may_close_task`, which reads False for `no` and `conditional` alike, so
  the class is now asserted by value across the whole registry table.

### Added
- **Delegation-authority hardening.** `GET /api/authority/{session_id}` and a
  delegation-aware `my_authority` report `base_authority`, the delegation
  `mode`, and the `effective_authority` intersection separately — reporting only
  the base is how a READ_ONLY child was told it could edit and commit.
  `delegation_status` gives the whole lifecycle (full delegation/parent/child
  ids, state, mode, child effective authority, timestamps, per-session status
  and lock counts) so a delegation can be verified without reading the database.
  `docs/authority-model.md` writes down the policy for every mutation surface.

### Fixed
- **A delegated child could not act on its own row.** `agent_label` appended a
  cid suffix, so a child whose ATS row says `claude-code:delegate` computed
  `claude-code:delegate:<cid8>` and failed the ownership check on itself. Under
  a delegation the label takes no suffix, and the mutation guard now accepts an
  exact delegation binding (this session IS that delegation's child) as
  authority in its own right, rather than comparing names.
- **Explicit-id surfaces had no authorization.** `reconcile_delegation` is now
  owner-bound (only the parent, and an unidentified actor is refused), returning
  a result is child-bound, `delete_lock` refuses a lock held by a live
  heartbeating session while keeping the ghost-reap path open, and
  `respond_to_request` only accepts the session the request is addressed to.
  `record_restart` stays intentionally global and session-optional.

### Added
- **Build identity and MCP catalog parity** (`build_info.py`, `GET /api/version`,
  `ats_version` MCP tool, `scripts/deploy.sh`, `scripts/check_mcp_parity.py`).
  A stdio MCP server is spawned once per client session and holds its tool
  catalog for that session's whole life, so `pipx install --force` updates REST
  (restarted) and leaves every running client on the old catalog. Observed
  2026-09-11: an independent Codex session whose MCP started 13:45:39 reported
  that delegation "was not testable — the catalog exposes no delegation
  operations" while REST had served `/api/delegations` since 14:01:58. Neither
  surface could state its own revision, so staleness was indistinguishable from
  a missing feature.
  Both surfaces now report commit, version, package path, pid and process start.
  `ats_version` fetches both and names a SKEW explicitly, telling the caller to
  restart its session rather than conclude a feature is absent. `deploy.sh`
  stamps the commit into the package before install, so the installed copy
  carries the revision it was built from. `check_mcp_parity.py` spawns the
  INSTALLED entrypoint and fails when its catalog is missing a tool this
  checkout registers, or when installed MCP and running REST disagree on commit.

### Fixed
- **Machine-facing MCP output no longer truncates ids.** `start_session`,
  `pause_session`, `resume_session`, `get_session_details`, lock creation,
  `reconcile_delegation` and the mutation-guard refusals returned 8-character
  prefixes, so recovering a canonical id meant scraping the database. Full ids
  now; CLI terminal display still abbreviates, which is display, not interface.

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
