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

## What this is not

The API is unauthenticated on loopback by design. A worker that wants to claim
another's session id can. These rules stop a worker exceeding its role by
accident, make roles declared and discoverable, and make a deliberate crossing
visible in the record. They are not an access-control boundary.
