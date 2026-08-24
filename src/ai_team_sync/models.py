"""SQLAlchemy models for sessions, scope locks, and decisions."""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone

from sqlalchemy import Boolean, DateTime, ForeignKey, Integer, String, Text
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

    session: Mapped[Session] = relationship(back_populates="locks")


class Decision(Base):
    """A design decision logged during a session."""

    __tablename__ = "decisions"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_new_id)
    session_id: Mapped[str] = mapped_column(ForeignKey("sessions.id", ondelete="CASCADE"))
    title: Mapped[str] = mapped_column(String(500))
    chosen: Mapped[str] = mapped_column(Text)
    rejected: Mapped[str | None] = mapped_column(Text, nullable=True)
    reasoning: Mapped[str] = mapped_column(Text, default="")
    files: Mapped[str] = mapped_column(Text, default="")  # JSON list of file paths
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)

    session: Mapped[Session] = relationship(back_populates="decisions")


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
