"""Pydantic schemas for API request/response models."""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, Field, StrictInt, field_validator


# --- Session ---

class SessionCreate(BaseModel):
    developer: str
    agent: str = "unknown"
    scope: list[str] = Field(default_factory=list)
    description: str = ""
    branch: str = ""
    auto_lock: bool = True  # auto-create scope locks from scope patterns
    lock_mode: str = "advisory"  # advisory (warn) or exclusive (block)
    # Absolute git root the session works in; anchors its repo-relative scope
    # patterns so they don't collide across repos. '' = unanchored (legacy).
    repo_root: str = ""
    # Set when this session IS a delegated child. Its authority is then the
    # intersection of its worker's and the delegation mode's — never the union.
    delegation_id: str = ""
    # The Tower task this session may close (#2741). Fixed at creation; an
    # ATS task_close grant is only ever for this id. Strict: "2741" is refused
    # rather than coerced, because it scopes authority.
    task_id: StrictInt | None = None
    ticket_id: StrictInt | None = Field(default=None, gt=0)


class HandoffCreate(BaseModel):
    verdict: str = Field(min_length=1, max_length=4000)
    blockers: list[str] = Field(default_factory=list)
    next_steps: list[str] = Field(default_factory=list)
    artifacts: list[str] = Field(default_factory=list)


class SessionUpdate(BaseModel):
    status: str | None = None  # active|paused|completed
    summary: str | None = None
    scope: list[str] | None = None
    description: str | None = None
    # ANCHORING AFTER THE FACT (#2554). Every session now begins auto-registered
    # by the SessionStart hook with repo_root='' (Gap 0), and start_session — the
    # only other writer of repo_root — cannot help, because by then the session
    # already exists. Without this field there was NO API PATH from the default
    # state to an anchored session: PATCH accepted repo_root, Pydantic dropped
    # it as an extra key, and the caller got a 200 describing a change that
    # never happened. Anchoring is not cosmetic — _uncommitted_in_scope returns
    # [] for an unanchored session, so an agent's dirty files stayed invisible
    # to the whole team, and the reaper completed it without ever surfacing them.
    repo_root: str | None = None
    handoff: HandoffCreate | None = None


class SessionResponse(BaseModel):
    id: str
    developer: str
    agent: str
    scope: list[str]
    description: str
    status: str
    branch: str
    repo_root: str = ""  # '' = unanchored (legacy session)
    ticket_id: int | None = None
    started_at: datetime
    completed_at: datetime | None = None
    last_heartbeat: datetime | None = None  # NULL = never heartbeated (see Gap 1)
    summary: str | None = None
    lock_count: int = 0
    decision_count: int = 0
    commit_count: int = 0
    # Diff visibility (ats-git-diff-merge-workflow-p01): the session's
    # uncommitted files that fall inside its scope (capped) — overlap between
    # sessions reads as diffs on the board, not just lock patterns.
    uncommitted_in_scope: list[str] = []
    # Liveness: seconds since the session's most recent activity, and whether it
    # has gone silent past the heartbeat window (a suspected ghost). Lets
    # team_status flag idle/ghost sessions so their scope isn't treated as a live
    # blocker. (ats-ghost-session-liveness-reap-p01)
    idle_seconds: float | None = None
    is_stale: bool = False
    # True when the REAPER completed this session rather than its owner. Surfaced
    # so a client can tell "I was reaped" (recoverable — heartbeat resurrects it)
    # from "I finished" (terminal), instead of both reading as plain 'completed'.
    auto_completed: bool = False
    # Resurrection result, STRUCTURED (#2760). The lifecycle marker carries only
    # counts and an outcome, because a lock pattern is arbitrary glob syntax and
    # narrative text is the wrong place to encode it. Anything that needs the
    # patterns reads them here, or off the session.resurrected event — never by
    # parsing `summary`.
    #
    # `restoration_outcome` is the STATE and is always set. It exists because
    # every distinct thing that can happen used to render as an empty refusal
    # string: "this request was not a revival", "it was, and there was nothing
    # to give back", and "it was, and something went wrong" were indistinguishable
    # to a caller.
    #   not_attempted      - this request did not revive anything
    #   restored           - revival ran; see locks_restored / locks_not_restored
    #   nothing_journalled - revival ran; the reaper had banked no locks
    #   refused            - revival ran; restoration refused, see restoration_reason
    #   concurrent         - another request holds the restoration claim
    restoration_outcome: str = "not_attempted"
    restoration_reason: str = ""    # '' | owner_completed | identity_bound |
                                    # unidentified_owner | anchor_moved |
                                    # invalid_journal
    restoration_detail: str = ""
    locks_restored: list[str] = []
    locks_not_restored: list[str] = []
    # Why each entry in `locks_not_restored` was not restored — 'newer_holder'
    # or 'expired'. PARALLEL to that list, index for index, and always the same
    # length: a count alone cannot tell an owner whether its lane was taken or
    # simply aged out, and a dict keyed by pattern could not answer it either
    # once a journal named the same lane twice for two different reasons.
    not_restored_reasons: list[str] = []

    model_config = {"from_attributes": True}


# --- Lock ---

class LockCreate(BaseModel):
    session_id: str
    pattern: str
    mode: str = "advisory"
    reason: str = ""  # human/agent-readable WHY; put prose HERE, never in `pattern`

    @field_validator("pattern")
    @classmethod
    def _pattern_must_be_a_glob_not_prose(cls, v: str) -> str:
        """A lock pattern must be a path glob, not a sentence.

        Agents (incl. me) stuffed descriptions into `pattern`; a sentence never
        fnmatch-matches a real path, so the lock silently protects nothing. Reject
        prose early and point the caller at `reason`.
        """
        v = (v or "").strip()
        if not v:
            raise ValueError("pattern must be a non-empty path glob (e.g. 'src/**', 'pkg/foo.py')")
        if " " in v or "\t" in v or "\n" in v:
            raise ValueError(
                "pattern looks like prose (contains whitespace); it must be a path glob "
                "like 'packages/scene_generation/builder.py' or 'src/**'. Put the description in `reason`."
            )
        if len(v) > 200:
            raise ValueError("pattern too long to be a glob; put the description in `reason`")
        return v


class LockCheckRequest(BaseModel):
    paths: list[str]
    session_id: str = ""
    # Caller's git root: places RELATIVE paths in that repository, so locks
    # anchored to another repository do not match them. Absolute paths carry their
    # own location. '' = relative paths match every repository's locks (legacy).
    # Contract: docs/lock-readers.md.
    repo_root: str = ""


class LockMatch(BaseModel):
    lock_id: str
    session_id: str
    agent: str
    developer: str
    mode: str
    pattern: str
    reason: str = ""
    is_own: bool = False
    override_granted: bool = False


class LockCheckResult(BaseModel):
    path: str
    locked: bool
    lock_id: str | None = None
    session_id: str | None = None
    developer: str | None = None
    mode: str | None = None
    pattern: str | None = None
    reason: str | None = None  # WHY the path is locked (surfaced so the blocker is actionable)
    agent: str | None = None
    matches: list[LockMatch] = Field(default_factory=list)
    caller_identity_unresolved: bool = False


class LockResponse(BaseModel):
    id: str
    session_id: str
    pattern: str
    reason: str = ""
    mode: str
    created_at: datetime
    expires_at: datetime
    developer: str | None = None
    agent: str | None = None
    repo_root: str = ""

    model_config = {"from_attributes": True}


# --- Decision ---

class DecisionCreate(BaseModel):
    session_id: str
    ticket_id: StrictInt | None = Field(default=None, gt=0)
    recipient_session_id: str | None = None
    title: str
    chosen: str
    rejected: str | None = None
    reasoning: str = ""
    files: list[str] = Field(default_factory=list)


class DecisionResponse(BaseModel):
    id: str
    session_id: str
    ticket_id: int | None = None
    recipient_session_id: str | None = None
    title: str
    chosen: str
    rejected: str | None = None
    reasoning: str
    files: list[str]
    created_at: datetime

    model_config = {"from_attributes": True}


# --- Commit ---

class CommitCreate(BaseModel):
    session_id: str
    commit_hash: str
    message: str = ""


class CommitResponse(BaseModel):
    id: str
    session_id: str
    commit_hash: str
    message: str
    created_at: datetime

    model_config = {"from_attributes": True}


# --- Override Request ---

class OverrideRequestCreate(BaseModel):
    requester_session_id: str
    conflicting_pattern: str
    justification: str = ""


class OverrideRequestResponse(BaseModel):
    id: str
    requester_session_id: str
    owner_session_id: str
    conflicting_pattern: str
    justification: str
    status: str  # pending|approved|denied|expired
    response_message: str | None = None
    created_at: datetime
    responded_at: datetime | None = None
    expires_at: datetime
    requester_developer: str | None = None
    owner_developer: str | None = None
    requester_agent: str | None = None
    owner_agent: str | None = None

    model_config = {"from_attributes": True}


class OverrideRequestRespond(BaseModel):
    approved: bool
    message: str = ""
    # The lock OWNER answers. An override is permission to cross someone's
    # claim, so the only session that can grant it is the one holding it.
    actor_session_id: str


# --- Presence (HTTP, for hook-driven auto-emit; WS path is for the live UI) ---

class PresenceUpdate(BaseModel):
    developer: str
    agent: str = "unknown"
    session_id: str = ""
    files: list[str] = Field(default_factory=list)
    intent: str = ""  # one-line WHAT they're doing


class PresenceEntry(BaseModel):
    developer: str
    agent: str
    session_id: str = ""
    files: list[str]
    intent: str = ""


class WhosEditingRequest(BaseModel):
    paths: list[str]
    exclude_session_id: str = ""
    exclude_developer: str = ""  # legacy: omit yourself by developer name
    exclude_agent: str = ""  # preferred: omit only YOUR session (per-session agent
    # label), so a concurrent same-developer session is still reported. Falls back to
    # exclude_developer when empty.


class WhosEditingResult(BaseModel):
    path: str
    editors: list[PresenceEntry] = Field(default_factory=list)  # others active on this path


# --- Service restart (#2559) ---

#: Recorded outcomes. Closed vocabulary, enforced where the write happens -- the
#: cheapest place to make a bad value impossible rather than merely rare.
#: 'in_progress' exists so a caller may record INTENT before bouncing a unit and
#: PATCH the result afterwards; it is optional, never required.
RESTART_OUTCOMES = ("completed", "failed", "in_progress")


def normalize_unit(unit: str) -> str:
    """Collapse the spellings of one systemd unit into a single queryable name.

    'comfyui.service', 'ComfyUI' and '  comfyui  ' are the same service, and if they
    land as three distinct strings then "when was comfyui last bounced" -- the whole
    question this record exists to answer -- silently returns a partial history.

    Normalizing at the PRODUCER is the lesson from shots.composition_method, which
    accumulated ~19 alias strings because each writer invented its own spelling and
    every reader was then expected to know them all.
    """
    return (unit or "").strip().lower().removesuffix(".service").strip()


class RestartCreate(BaseModel):
    unit: str
    session_id: str | None = None  # absent for an operator's out-of-band restart
    developer: str = ""
    reason: str = ""
    outcome: str = "completed"
    old_pid: int | None = None
    new_pid: int | None = None
    before: dict = Field(default_factory=dict)
    after: dict = Field(default_factory=dict)

    @field_validator("unit")
    @classmethod
    def _normalize(cls, v: str) -> str:
        v = normalize_unit(v)
        if not v:
            raise ValueError("unit must be a systemd unit name, e.g. 'comfyui'")
        return v

    @field_validator("outcome")
    @classmethod
    def _known_outcome(cls, v: str) -> str:
        v = (v or "").strip().lower()
        if v not in RESTART_OUTCOMES:
            raise ValueError(f"outcome must be one of {', '.join(RESTART_OUTCOMES)}")
        return v


class RestartUpdate(BaseModel):
    """Enrich a recorded restart after the fact.

    `after` is separate from `before` because "did it help" can only be measured
    once the unit has settled -- recycling ComfyUI-NVIDIA moved RAM 22.7 -> 47.7 GB,
    a number that does not exist at the moment the restart is issued.
    """

    outcome: str | None = None
    after: dict | None = None
    reason: str | None = None
    new_pid: int | None = None

    @field_validator("outcome")
    @classmethod
    def _known_outcome(cls, v: str | None) -> str | None:
        if v is None:
            return v
        v = v.strip().lower()
        if v not in RESTART_OUTCOMES:
            raise ValueError(f"outcome must be one of {', '.join(RESTART_OUTCOMES)}")
        return v


class RestartResponse(BaseModel):
    id: str
    unit: str
    session_id: str | None = None
    developer: str = ""
    agent: str = ""
    reason: str = ""
    outcome: str
    old_pid: int | None = None
    new_pid: int | None = None
    before: dict = Field(default_factory=dict)
    after: dict = Field(default_factory=dict)
    created_at: datetime
    #: Age computed SERVER-side, mirroring SessionResponse.idle_seconds. SQLite
    #: stores no UTC offset, so created_at serializes naive ('...T23:42:27') even
    #: though the column is DateTime(timezone=True); a client that parsed it and
    #: compared against an aware now() would raise TypeError. "bounced 3 minutes
    #: ago" is the whole point, so the arithmetic belongs where the tzinfo is known.
    age_seconds: float = 0.0
