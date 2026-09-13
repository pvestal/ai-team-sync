# ATS authority model

Who may change what, and on what evidence. Written down because the surfaces
have genuinely different semantics and running them all through one guard would
break the ones that are meant to be open.

## Two kinds of operation

**Identity-resolved.** The caller does not name a target; the server works out
which session the caller *is* and mutates that. Everything here depends on the
identity being provably process-local, so these go through `mutation_refusal`:

| Operation | Policy |
|---|---|
| `pause_session` / `resume_session` | own session, proven identity |
| `extend_scope` | own session, proven identity |
| `log_decision` | own session, proven identity |
| `request_override` | own session, proven identity |

Proven identity means: the session this MCP process started (in-memory), an
explicit `ATS_SESSION_ID`, this Claude session's own pointer file, or an exact
delegation binding naming this session as a delegation's child. The shared
`~/.ats_session` file is **never** proof, because any agent on the box can write
it — see `session_pointer.resolve_pointer_source`.

**Explicit-id.** The caller names the target. Identity resolution is irrelevant;
the question is whether the caller is entitled to act on *that* object.

| Operation | Class | Policy |
|---|---|---|
| `reconcile_delegation` (close/reject) | owner-bound | only `parent_session_id`; an unidentified actor is refused |
| delegation `return` | child-bound | only `child_session_id` submits the result |
| `delete_lock` | owner-bound, reap path open | refused while the owner is active AND heartbeating; a ghost's lock stays reapable |
| `respond_to_request` | owner-bound | only the session the request is addressed to |
| `record_restart` | intentionally global, session-optional | a shared-service bounce is worth recording even unattributed; attribution is added when a session is known |
| `complete_session(session_id=…)` | owner-bound | the named session must be yours: existence and ownership are settled first, and only then is a conflicting pointer considered |

### `complete_session` is in both tables, deliberately

It is the one surface with two paths, because it had to gain an explicit target
without breaking callers that never passed one.

**With `session_id`** it is explicit-id: that id is authoritative for the call.
Existence and ownership settle *before* any pointer question, so a missing
session is refused as missing and another worker's session is refused as theirs.
Only then does a pointer that speaks for *this caller* — `in_process`, `env` or
`per_session` — and names a **different live** session refuse the call rather
than tiebreak it. A stale pointer naming a finished session yields to the
explicit id, with the discrepancy reported.

The shared `~/.ats_session` never speaks for the caller, so it cannot create
that conflict. That distinction is load-bearing: a delegated Codex child
completing itself while the global file still names its Claude parent is *not* a
conflict, and treating it as one would refuse the case isolation exists to
support.

**Without `session_id`** it is the legacy identity-resolved path, unchanged: the
pointer resolves the target and the shared file is still refused outright. The
result names the resolved id either way, so a caller that passed nothing can
still see which row changed.

A terminal session is reported, not re-patched. The endpoint is not idempotent —
a second completion re-stamps `completed_at` and re-emits the completion event —
so re-completing would invent a second closure of one session.

## Delegated authority

Authority under a delegation is the **intersection** of the worker registry's
grant and the mode's envelope. A mode can never grant what the worker lacks, or
delegation becomes the escalation path around the registry.

`GET /api/authority/{session_id}` reports all three separately: `base_authority`,
the `delegation` mode, and `effective_authority`. Reporting only the base is how
a READ_ONLY child gets told it may edit and commit; reporting only the effective
hides why it may not.

## Caller identity and mutation grants (#2741)

A worker label SELECTS a class. For every builtin class — `claude-code`,
`codex`, `default`, `local` — nothing proves the caller is entitled to it, so
that class's authority is coordination policy. ATS never grants an
authoritative mutation on it.

**Identity-bound classes.** A registry entry may declare `bind_users` (or
`bind_uids`). Binding happens once, at `POST /api/sessions`: the server reads
the uid that owns the client end of the connection from `/proc/net/tcp{,6}`,
matching the exact 4-tuple in ESTABLISHED state, and grants the class only if
that uid is one of the bound accounts. Otherwise the session is `restricted`.
The result is written to the session (`creator_uid`, `bound_worker`,
`bound_uid`, `task_id`) and is never re-derived from the label.

A request carrying any forwarding header (`X-Forwarded-For`, `Forwarded`, ...)
is unidentifiable. uvicorn's default trusts that header from 127.0.0.1 and
rewrites the client address and port to whatever it names; that was
demonstrated to turn a uid-1000 caller into uid 993. `ats-server` also disables
proxy headers.

**Grants.** `POST /api/authority/{session_id}/authorize` with
`{action, repo_root, paths, task_id, evidence}` answers `allowed` plus every
reason it is not. It always answers 200 and always writes `authority_checks`.
Allowed requires ALL of:

- the registry file was accepted, the session exists and is `active`
- the session was bound at creation, the current registry still binds that
  class to that uid, and THIS request comes from that uid
- delegation, if any: the one recorded on the session at creation and still
  pointing at it, `open`, unexpired, and its parent is an active bound session
  on the same uid; authority is worker ∩ mode ∩ parent. A delegation's child
  is set once, while open, by the account that owns the live parent
- `commit` / `land`: that capability, `edit=claimed_scope`, `repo_root` equal
  to the session's anchor, every path canonical (no glob characters, `..`,
  absolute form, backslash, pathspec magic or `.git`; `//` and `.` collapse),
  inside the repository after symlinks, covered — as spelled AND as resolved —
  by one of the session's **authority-bearing** live claims, and not possibly
  under an exclusive lock of another live session (expired or not). Locks are
  compared conservatively in absolute space with each lock's own anchor
  applied, so a lock anchored at a subdirectory, a parent or a symlinked
  spelling of the repository still applies, and symlinks are resolved on both
  sides. For `land`, each path is also compared as the same file in every
  checkout of the repository (main root and every registered worktree), so an
  exclusive lock on it in any checkout — however that lock is anchored —
  applies; a `commit` in an isolated worktree is not blocked by another
  checkout's lock. Advisory locks — the default lock mode — never block
  a grant
- `task_close`: `task_close` of `yes`/`conditional`, `task_id` equal to the
  task the session declared at creation, and for `conditional` evidence with at
  least one non-empty value (its content is the caller's gate to judge)

`land` is separate from `commit` and no builtin or delegation mode holds it.

**Claims cannot be self-granted.** Only the locks a bound session creates at
creation, from its bound account, in canonical form (exact file or `dir/**`),
bear authority. `POST /api/locks` still creates coordination locks for anyone
who owns the session, but they never bear authority.

**Cross-account changes.** Sessions record the uid that created them. A live
session — however long it has been silent — cannot be completed, patched,
re-anchored, have locks attached, have its locks deleted, or have a delegation
child opened under it by a different or unidentifiable uid; another account's
ghost is left to the in-process reaper. For sessions whose creator was never
identified (older rows), unidentifiable callers and headless bound accounts are
refused and ordinary identified accounts behave as before. An identity-bound
session cannot be reopened (PATCH or heartbeat) and its anchor cannot move.
Liveness follows the same rule on every path that proves it — the heartbeat
endpoint, `X-ATS-Session-Id` / `X-ATS-Agent` headers and `POST /api/presence`
refresh only sessions the requesting account owns — so no account can move
another's session onto the fast reaper window. Creating a delegation under a
session, returning a child's result, and closing a delegation are likewise
refused from another account. The TTL sweep does not delete an exclusive lock
while its owner is live (active, or paused and not silent past the heartbeat
window), and such a lock stays visible in `/api/locks` and in every conflict
check, not only to grants.
A same-account client that reaches ATS unidentifiably (through a proxy, or
with a forwarding header) is refused these operations on its own live sessions.

**How long conflict protection lasts.** Exactly as long as ATS itself considers
the lock's owner live. A session the reaper completes — silent past the
heartbeat window after it has heartbeated, or past the inactivity window if it
never did — releases its locks for everyone, and from then on they no longer
block a grant. No other account can shorten that; it is the operator's reaper
policy, not a per-grant decision.

**Configuration.** The registry file is parsed strictly: unknown keys, non-bool
booleans (`"false"`), non-list or non-integer uids, unknown accounts, binding to
root or to ATS's own uid, and an unbound class under a bound family are all
errors. A rejected file is rejected whole: builtins stay in force so
coordination does not wedge, and no identity-bound class exists, so nothing is
granted.

**What it proves and what it does not.** It distinguishes OS accounts. It does
not stop root or anything root-equivalent (membership of the `docker` group is),
and it does not distinguish two processes of the bound account: every piece of
code running as that account can obtain the class, including code the worker
itself executes. It does not authenticate Claude or Codex: they run as the same
uid as every other interactive process, so their authority stays advisory.

## What this is not

The API is unauthenticated on loopback by design. For unbound classes a worker
that wants to claim another's name or session id can. These rules stop a worker
exceeding its role by accident, make roles declared and discoverable, and make a
deliberate crossing visible in the record. Identity-bound grants are the
exception described above, and only to the extent described there.
