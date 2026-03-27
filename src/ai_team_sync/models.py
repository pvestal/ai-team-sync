"""SQLAlchemy models for sessions, scope locks, and decisions."""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone

from sqlalchemy import DateTime, ForeignKey, String, Text
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
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    summary: Mapped[str | None] = mapped_column(Text, nullable=True)

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
    pattern: Mapped[str] = mapped_column(String(500))  # glob pattern
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
