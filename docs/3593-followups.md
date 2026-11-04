# #3593 follow-ups: deliberately out of scope

#3593 makes every session keep its own capability (the keyring) and lets a
subagent register its own session beside its parent's. These adjacent problems
were found while doing that and were left alone on purpose: each changes a
contract that other tickets own, or needs a server-side change #3593 does not
make. Each entry gives the dependency to settle first.

## 1. Cross-session message acknowledgement and forwarding

**Now:** a message is delivered to exactly one session. The MCP nudge that
follows every tool call reads only the process's DEFAULT session's inbox
(`call_tool` -> `_incoming_message_nudge(resolve_identity())`), and the
`override_inbox` prompt hook reads only the pointer's session. A non-default
session (a second repository's session, a registered subagent) sees its mail
only when it calls `message_inbox(session_id=<own id>)`. When a subagent
completes, its unread direct messages are not forwarded to its parent.

**Open questions:** should the nudge cover every OPEN session this process
holds (one GET per session per tool call)? Should a subagent's completion
readdress its unread mail to its parent automatically, or only offer it?

**Depends on:** the turnover/readdress rules (`routers/messages.readdress_message`,
#2907), ticket-mailbox ownership (#3585 / collab-s1), and the decision that a
completed sender keeps its readdress capability (kept by #3593).

## 2. Automatic lock transfer between parent and subagent

**Now:** none, by contract. A subagent claims its own scope; completing either
session releases exactly that session's locks; nothing moves between them.

**Why not here:** there is no server route that moves a lock atomically. A
client-side DELETE + POST races every other claimant for the lane, and a
re-claim of a lane the session still holds is refused (`lane_already_held`,
#2765). A transfer must also carry the #2760 restoration journal correctly.

**Depends on:** a server-side `POST /api/locks/{id}/transfer` (owner capability
of both sides, same OS account, journal semantics), #2765 dedupe, #2760.

## 3. Reaper / heartbeat resurrection of non-default sessions

**Now:** liveness comes from (a) the MCP heartbeat on every tool call, sent for
the session that call acts as, and (b) the `session_heartbeat` hook, which beats
the POINTER's session. A held session that is neither the default nor named in
a recent call (an idle second-repository session, a quiet subagent) gets no
heartbeat and is auto-completed by the reaper after
`session_heartbeat_timeout_minutes`. `session_registration._reuse_existing`
heartbeats an auto-reaped POINTER session back to life; nothing does that for
the other held sessions. Locally such a session's keyring entry stays `open`
(the state records only this client's own results), so it is never evicted.

**Open questions:** should the heartbeat hook beat every open session in this
client's keyring? Should a reaped (`auto_completed`) entry be marked locally,
and by whom, given the rule that local state records only this client's own
results?

**Depends on:** the reaper/heartbeat CAS (#2811), the one-door restoration rule
(#2760: only a heartbeat may revive a reaped session with a journal), and
`liveness.py`.

## 4. Multi-repository sessions and verdict formats

**Now:** a session has one `repo_root`; work spanning two repositories is two
sessions in one MCP process, which #3593 makes safe (each keeps its
capability). Handoffs and structured verdicts (`verdicts.py`, #3585) are
per-session, so a change that spans two repositories ends with two handoffs
and two verdicts with no link between them.

**Open questions:** is a multi-root session wanted at all, or a "linked
sessions" record? What does a VERIFY verdict for a two-repository change look
like?

**Depends on:** repository anchoring (`ats-lockcheck-repo-anchoring-p01`),
work admission (#3591: one claim, one repository), verdict schema (#3585).

## 5. Codex inbox and capability behaviour

**Now:** the keyring is keyed by the client id `session_pointer.claude_session_id()`
returns. Codex has no `CLAUDE_CODE_SESSION_ID`; its SessionStart hook keys its
lifecycle by `lifecycle_session_key()` (a hash of the Codex thread id), and its
MCP server identifies itself by an in-process token (`codex:<instance>`). Not
verified as part of #3593: whether a Codex MCP process and its hooks resolve
the SAME client id, i.e. whether the Codex `override_inbox`/prompt hooks can
find the capability of a session the Codex MCP process started. If they do
not, Codex keeps the pre-#3593 behaviour for hooks (capability held in the MCP
process only), which is safe (fails closed) but loses hook-side inbox reads.

**Depends on:** `hooks/codex_session_autostart.py`, `session_registration.lifecycle_session_key`,
and the Codex MCP identity rule in `mcp/server.session_agent_label`.

## Also noted, not changed

* **Labels still carry `cid[:8]`.** `claude-code:<cid8>` is the server-visible
  agent label the lock guard, the console and #2893 attribution match on.
  Changing it changes identity on the board and in admission; #3593 instead
  stopped relying on the label alone (the sibling rule now also requires the
  sibling's capability in this client's keyring). The live-cid record is keyed
  by the Claude PID, not by `cid[:8]`, and was not changed.
* **`delete_lock` and `reconcile_delegation` are not capability-checked on the
  server.** They trust `actor_session_id` (plus the OS-account check). The MCP
  now names an actor only when it holds that session's capability, and sends
  that capability, which the server ignores today. A server-side check would
  be a server contract change.
