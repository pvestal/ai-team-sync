"""SQLAlchemy models for sessions, scope locks, and decisions."""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone

from sqlalchemy import Boolean, DateTime, ForeignKey, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship

from ai_team_sync.config import settings


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _default_expiry() -> datetime:
    return datetime.now(timezone.utc) + timedelta(hours=settings.lock_ttl_hours)


def _new_id() -> str:
    return str(uuid.uuid4())


class Base(DeclarativeBase):
    pass


class Session(Base):
    """An AI-assisted working session declared by a developer."""

    __tablename__ = "sessions"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_new_id)
    developer: Mapped[str] = mapped_column(String(255))
    agent: Mapped[str] = mapped_column(String(100), default="unknown")
    scope: Mapped[str] = mapped_column(Text, default="")  # JSON list of glob patterns
    description: Mapped[str] = mapped_column(Text, default="")
    status: Mapped[str] = mapped_column(String(20), default="active")  # active|paused|completed
    branch: Mapped[str] = mapped_column(String(255), default="")
    # Repo anchoring (ats-lockcheck-repo-anchoring-p01): scope patterns are
    # repo-RELATIVE globs ('tests/**'), so without knowing WHICH repo a session
    # works in, 'tests/**' held for /opt/anime-studio false-blocks edits to
    # ~/code/ai-team-sync/tests/. '' = unanchored (legacy) -> enforced everywhere.
    repo_root: Mapped[str] = mapped_column(String(1024), default="")
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    summary: Mapped[str | None] = mapped_column(Text, nullable=True)
    # Liveness signal (nullable). A live client POSTs /sessions/{id}/heartbeat
    # periodically; the reaper uses it for a FAST cleanup path so a dead Claude
    # process's locks don't linger the full inactivity window. NULL = this session
    # never heartbeated -> reaper falls back to the conservative session_inactivity_hours
    # derived-activity rule, so legacy/non-heartbeating clients are unaffected. See
    # docs/product-gaps-reaper-and-scope.md Gap 1.
    last_heartbeat: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    # TRUE when the REAPER completed this session, not the operator. The two are
    # not interchangeable: a heartbeat arriving after an auto-completion is proof
    # the reaper guessed wrong and the process is alive, so the session is
    # resurrected; a heartbeat after an OPERATOR completion is a late hook from a
    # process shutting down and must not reopen work the operator called done.
    # Before this flag the only marker was a substring in `summary`, which is not
    # something a security-relevant branch should read.
    auto_completed: Mapped[bool] = mapped_column(
        Boolean, default=False, nullable=False, server_default="0")
    # Caller identity, recorded once at creation and never re-derived (#2741).
    # creator_uid: the kernel's owner of the creating connection; NULL = not
    #   identifiable (legacy rows, in-process test transports).
    # bound_worker / bound_uid: set only when the agent's class is identity-bound
    #   AND the creator is one of its accounts. '' / NULL = unbound, which is
    #   every interactive client; unbound sessions never receive a mutation grant.
    # task_id: the one Tower task this session may close, declared at creation.
    creator_uid: Mapped[int | None] = mapped_column(Integer, nullable=True)
    approval_token_hash: Mapped[str] = mapped_column(String(64), default="")
    bound_worker: Mapped[str] = mapped_column(String(100), default="")
    bound_uid: Mapped[int | None] = mapped_column(Integer, nullable=True)
    task_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    # Coordination lineage only. Never interpreted as task-close authority.
    ticket_id: Mapped[int | None] = mapped_column(Integer, nullable=True, index=True)
    # The delegation this session was created as the child of, recorded at
    # creation. Delegation.child_session_id is a pointer on another row; a grant
    # must never depend on it alone, or moving it moves the narrowing.
    delegation_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    # The locks the REAPER deleted, as JSON, so resurrection can re-take the lane
    # it was wrongly stripped of (#2760). Written at reap, CLAIMED atomically and
    # cleared at resurrection. It records what was HELD, never what `scope`
    # declared — scope is a declaration, a lock is a grant.
    #
    # The envelope is an object, not a bare list, because a lock is pinned in
    # SPACE as well as time: `{"repo_root": <canonical>, "locks": [...]}`. A
    # pattern is only meaningful inside the repo it was authored against, so the
    # anchor travels with it and restoration refuses when the session has been
    # re-anchored since (#2760 F4). An owner completion empties this field
    # permanently — see routers.sessions.update_session (#2760 F1).
    reaped_locks: Mapped[str] = mapped_column(Text, default="")
    # The lanes resurrection REFUSED to give back, as a JSON list of patterns
    # (#2760 item 3). Durable because the authority question outlives the request
    # that answered it: the heartbeat response carries `locks_not_restored`, but
    # the Stop hook and the MCP both discard the body, so without this the only
    # record of a lane being gone died with the response — and `scope`, which
    # still names that lane, went on reading as a live claim to the PreToolUse
    # guard and as held authority on the board.
    #
    # It is NOT authority and never grants anything: it exists so the guard, the
    # board and the owner agree with the locks table about what is NOT held. A
    # lane re-taken later needs no write here — every reader subtracts the locks
    # actually held, so a real lock always wins over this record of its absence.
    locks_not_restored: Mapped[str] = mapped_column(Text, default="")

    locks: Mapped[list[ScopeLock]] = relationship(back_populates="session", cascade="all, delete-orphan")
    decisions: Mapped[list[Decision]] = relationship(back_populates="session", cascade="all, delete-orphan")
    commits: Mapped[list[CommitRecord]] = relationship(back_populates="session", cascade="all, delete-orphan")
    override_requests_sent: Mapped[list[OverrideRequest]] = relationship(
        back_populates="requester_session", foreign_keys="OverrideRequest.requester_session_id"
    )
    override_requests_received: Mapped[list[OverrideRequest]] = relationship(
        back_populates="owner_session", foreign_keys="OverrideRequest.owner_session_id"
    )


class ScopeLock(Base):
    """A lock on a file path pattern, tied to a session."""

    __tablename__ = "scope_locks"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_new_id)
    session_id: Mapped[str] = mapped_column(ForeignKey("sessions.id", ondelete="CASCADE"))
    pattern: Mapped[str] = mapped_column(String(500))  # glob pattern (a real path glob, NOT prose)
    # Human/agent-readable WHY for this lock. Added 2026-06-24: agents were stuffing
    # prose into `pattern`, which silently never fnmatch-matches a real path (the lock
    # then protects nothing) and makes the board illegible. Prose goes here; pattern
    # stays a glob (enforced by LockCreate validation).
    reason: Mapped[str] = mapped_column(Text, default="")
    mode: Mapped[str] = mapped_column(String(20), default="advisory")  # advisory|exclusive
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_default_expiry)
    # TRUE only for a claim an identity-bound session made at creation, in
    # canonical form. A mutation grant is measured against these and nothing
    # else, so a lock added later through POST /api/locks — by anyone, including
    # the session itself — can coordinate but can never confer authority.
    authority_bearing: Mapped[bool] = mapped_column(
        Boolean, default=False, nullable=False, server_default="0")

    session: Mapped[Session] = relationship(back_populates="locks")


class Decision(Base):
    """A design decision logged during a session."""

    __tablename__ = "decisions"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_new_id)
    session_id: Mapped[str] = mapped_column(ForeignKey("sessions.id", ondelete="CASCADE"))
    ticket_id: Mapped[int | None] = mapped_column(Integer, nullable=True, index=True)
    recipient_session_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    title: Mapped[str] = mapped_column(String(500))
    chosen: Mapped[str] = mapped_column(Text)
    rejected: Mapped[str | None] = mapped_column(Text, nullable=True)
    reasoning: Mapped[str] = mapped_column(Text, default="")
    files: Mapped[str] = mapped_column(Text, default="")  # JSON list of file paths
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)

    session: Mapped[Session] = relationship(back_populates="decisions")


class AgentMessage(Base):
    """Durable, session-addressed instruction with an explicit receipt."""

    __tablename__ = "agent_messages"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_new_id)
    sender_session_id: Mapped[str] = mapped_column(String(36), index=True)
    recipient_session_id: Mapped[str | None] = mapped_column(String(36), index=True, nullable=True)
    ticket_id: Mapped[int | None] = mapped_column(Integer, nullable=True, index=True)
    kind: Mapped[str] = mapped_column(String(20), default="message")
    handoff_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    sender_agent: Mapped[str] = mapped_column(String(100))
    sender_developer: Mapped[str] = mapped_column(String(255))
    recipient_agent: Mapped[str] = mapped_column(String(100), default="")
    body: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)
    acknowledged_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class Handoff(Base):
    """Structured verdict that survives session completion and reaping."""

    __tablename__ = "handoffs"
    __table_args__ = (UniqueConstraint("source_session_id"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_new_id)
    ticket_id: Mapped[int] = mapped_column(Integer, index=True)
    source_session_id: Mapped[str] = mapped_column(String(36), index=True)
    recipient_session_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    verdict: Mapped[str] = mapped_column(Text)
    blockers: Mapped[str] = mapped_column(Text, default="[]")
    next_steps: Mapped[str] = mapped_column(Text, default="[]")
    artifacts: Mapped[str] = mapped_column(Text, default="[]")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)


class CommitRecord(Base):
    """A commit made during a session, auto-logged by post-commit hook."""

    __tablename__ = "commit_records"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_new_id)
    session_id: Mapped[str] = mapped_column(ForeignKey("sessions.id", ondelete="CASCADE"))
    commit_hash: Mapped[str] = mapped_column(String(40))
    message: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)

    session: Mapped[Session] = relationship(back_populates="commits")


class ServiceRestart(Base):
    """A restart of a SHARED service, recorded so other sessions can see it (#2559).

    ATS claims FILES (scope_locks) and WORK (tower tasks) but claimed nothing for
    shared services, even though bouncing comfyui / anime-studio / tower-echo-brain
    is among the highest-blast-radius actions on this box: it drops queued prompts,
    kills in-flight renders, and deploys whatever happens to be on disk.

    Before this, a restart could only be logged as a generic Decision -- and
    team_status renders only `Decisions: N`, a count, so it was invisible to every
    other session. The one such record in the live DB carries "old PID 1969400 ->
    2415302" as prose inside `reasoning`, which no query can reach.

    NOT a ScopeLock. A unit name passes LockCreate's glob validator yet can never
    fnmatch a real path, so a service claim borrowed from that table would be an
    inert lock that silently protects nothing -- the same class of bug the `reason`
    column was added to fix, in a shape the validator cannot detect.

    This table RECORDS; it does not gate. Refusing a restart against a claimed unit
    is a separate decision precisely because a guard that makes an emergency recycle
    harder than going out-of-band is worse than no guard at all.
    """

    __tablename__ = "service_restarts"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_new_id)
    # Normalized at the writer (see schemas.RestartCreate): 'comfyui.service',
    # 'ComfyUI' and ' comfyui ' all land as 'comfyui'. Alias drift is prevented at
    # the producer rather than taught to every reader.
    unit: Mapped[str] = mapped_column(String(100), index=True)
    # Nullable and SET NULL, never CASCADE: the restart OUTLIVES the session that
    # did it. Sessions are reaped routinely, and "who bounced comfyui an hour ago"
    # must survive that. NULL also covers the most important case -- the operator
    # restarting something by hand, with no session at all.
    session_id: Mapped[str | None] = mapped_column(
        ForeignKey("sessions.id", ondelete="SET NULL"), nullable=True)
    developer: Mapped[str] = mapped_column(String(255), default="")
    reason: Mapped[str] = mapped_column(Text, default="")
    outcome: Mapped[str] = mapped_column(String(20), default="completed")
    old_pid: Mapped[int | None] = mapped_column(Integer, nullable=True)
    new_pid: Mapped[int | None] = mapped_column(Integer, nullable=True)
    # JSON objects as Text, matching how Session.scope stores its list. Free-form
    # on purpose: the metric that proves a restart helped differs per unit (queue
    # depth and VRAM for comfyui, commits-behind for anime-studio), and a fixed
    # column set would force every caller into the wrong shape.
    before_state: Mapped[str] = mapped_column(Text, default="{}")
    after_state: Mapped[str] = mapped_column(Text, default="{}")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)


class AuthorityCheck(Base):
    """One answer to "may this caller perform this authoritative mutation now?"

    Written for every POST /api/authority/{session}/authorize — allowed, refused,
    malformed or naming no session — so a mutation a headless worker performs can
    be traced to the grant it was made under, and a refusal is on the record
    rather than only in the worker's log. session_id is a plain column, not a
    foreign key: the audit row outlives the session. Evidence CONTENT is not
    stored, only its keys.
    """

    __tablename__ = "authority_checks"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_new_id)
    session_id: Mapped[str] = mapped_column(String(36), index=True)
    agent: Mapped[str] = mapped_column(String(100), default="")
    worker: Mapped[str] = mapped_column(String(100), default="")
    bound_worker: Mapped[str] = mapped_column(String(100), default="")
    bound_uid: Mapped[int | None] = mapped_column(Integer, nullable=True)
    peer_uid: Mapped[int | None] = mapped_column(Integer, nullable=True)
    action: Mapped[str] = mapped_column(String(40), default="")
    repo_root: Mapped[str] = mapped_column(String(1024), default="")
    paths: Mapped[str] = mapped_column(Text, default="[]")          # JSON list
    task_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    delegation_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    evidence_keys: Mapped[str] = mapped_column(Text, default="[]")  # JSON list
    allowed: Mapped[bool] = mapped_column(Boolean, default=False)
    reasons: Mapped[str] = mapped_column(Text, default="[]")        # JSON list
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)


class FileActivity(Base):
    """A file action reported by an instrumented client, not inferred from git."""

    __tablename__ = "file_activities"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_new_id)
    session_id: Mapped[str] = mapped_column(String(36), index=True)
    agent: Mapped[str] = mapped_column(String(100))
    developer: Mapped[str] = mapped_column(String(255))
    action: Mapped[str] = mapped_column(String(10))
    path: Mapped[str] = mapped_column(String(1024))
    repo_root: Mapped[str] = mapped_column(String(1024), default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)


class OverrideRequest(Base):
    """Request to override a lock conflict - enables agent-to-agent coordination."""

    __tablename__ = "override_requests"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_new_id)
    requester_session_id: Mapped[str] = mapped_column(ForeignKey("sessions.id", ondelete="CASCADE"))
    owner_session_id: Mapped[str] = mapped_column(ForeignKey("sessions.id", ondelete="CASCADE"))
    conflicting_pattern: Mapped[str] = mapped_column(String(500))  # The pattern that conflicts
    justification: Mapped[str] = mapped_column(Text, default="")  # Why override is needed
    status: Mapped[str] = mapped_column(String(20), default="pending")  # pending|approved|denied|expired
    response_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)
    responded_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    expires_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc) + timedelta(minutes=15)  # 15-min response window
    )

    requester_session: Mapped[Session] = relationship(
        back_populates="override_requests_sent", foreign_keys=[requester_session_id]
    )
    owner_session: Mapped[Session] = relationship(
        back_populates="override_requests_received", foreign_keys=[owner_session_id]
    )


class Delegation(Base):
    """A bounded subproblem handed from one worker to another.

    Deliberately NOT a handoff: `parent_session_id` keeps owning the work for
    the whole life of the child, and nothing in the child's lifecycle writes to
    the parent. `mode` is enforced (see delegation.effective_authority and
    launch_spec.build_launch), not merely recorded.
    """

    __tablename__ = "delegations"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_new_id)
    # The parent keeps ownership. Named for the invariant so a reader of this
    # row cannot mistake a returned child for a transfer.
    parent_session_id: Mapped[str] = mapped_column(
        ForeignKey("sessions.id", ondelete="CASCADE"))
    parent_task: Mapped[str] = mapped_column(String(120), default="")
    delegating_worker: Mapped[str] = mapped_column(String(100), default="")
    # The worker that was REQUESTED. Kept under its original name because every
    # existing reader uses it, but it is a request, never evidence of what ran.
    delegated_worker: Mapped[str] = mapped_column(String(100), default="")
    # What actually ran: the absolute executable the PARENT resolved before
    # spawning. This is the identity field, and delegated_worker is not. Before
    # it existed, `--worker codex` recorded a satisfied Codex delegation that a
    # Claude process had performed (2026-09-12, delegations 82fb4676/5c04aa74).
    # A child cannot influence this: delegation.child_env force-sets ATS_AGENT,
    # so any self-report is the parent's own label read back.
    resolved_binary: Mapped[str] = mapped_column(String(1024), default="")
    # Which launch contract produced that argv, so a record stays auditable
    # after the contract moves.
    launch_spec_version: Mapped[str] = mapped_column(String(20), default="")
    mode: Mapped[str] = mapped_column(String(20), default="READ_ONLY")
    repo_root: Mapped[str] = mapped_column(String(1024), default="")
    scope: Mapped[str] = mapped_column(Text, default="[]")        # JSON list
    objective: Mapped[str] = mapped_column(Text, default="")
    acceptance: Mapped[str] = mapped_column(Text, default="")
    prohibitions: Mapped[str] = mapped_column(Text, default="[]")  # JSON list
    child_session_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    lease_expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True),
                                                       default=_utcnow)
    # open -> returned -> closed | rejected | expired
    state: Mapped[str] = mapped_column(String(20), default="open")
    result_summary: Mapped[str] = mapped_column(Text, default="")
    evidence: Mapped[str] = mapped_column(Text, default="{}")      # JSON object
    verdict: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)
    closed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True),
                                                       nullable=True)
