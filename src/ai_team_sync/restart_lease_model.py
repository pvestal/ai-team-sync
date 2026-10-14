"""The restart lease table (#3580), registered on the shared declarative Base.

Kept out of models.py on purpose: the table is new and self-contained, and
models.py is under concurrent edit on other branches. Importing this module
(routers/restart_leases.py does, and server.py imports that router) is what
puts the table in Base.metadata, so init_db's create_all creates it.

ONE ACTIVE LEASE PER UNIT is enforced by the database, not by a check-then-
insert: `active_unit` holds the unit name while the lease is live and NULL once
it reaches a terminal state, under a UNIQUE constraint. NULLs never collide (in
SQLite or PostgreSQL), so terminal rows accumulate as history while two live
leases for one unit cannot both commit -- whichever INSERT loses gets an
IntegrityError, which the router turns into a recorded 409.
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import DateTime, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from ai_team_sync.models import Base, _new_id, _utcnow

#: probing -> granted -> draining -> cleared -> completed, or to a terminal
#: refused / released / expired. Only the first four hold the unit.
ACTIVE_STATES = ("probing", "granted", "draining", "cleared")
TERMINAL_STATES = ("refused", "released", "expired", "completed")


class RestartLease(Base):
    __tablename__ = "restart_leases"
    __table_args__ = (UniqueConstraint("active_unit", name="uq_restart_leases_active_unit"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_new_id)
    unit: Mapped[str] = mapped_column(String(100), index=True)
    active_unit: Mapped[str | None] = mapped_column(String(100), nullable=True)
    state: Mapped[str] = mapped_column(String(20), default="probing")
    # Plain column, not a foreign key: the lease is evidence and outlives the
    # session that took it (same reasoning as AuthorityCheck.session_id).
    session_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    developer: Mapped[str] = mapped_column(String(255), default="")
    reason: Mapped[str] = mapped_column(Text, default="")
    ttl_s: Mapped[int] = mapped_column(Integer, default=360)
    # 'normal' (drained) or 'bootstrap' (first load of the middleware; no drain).
    mode: Mapped[str] = mapped_column(String(20), default="normal")
    # JSON object class -> reason: the evidence this unit's gate does NOT check.
    waivers: Mapped[str] = mapped_column(Text, default="{}")
    refusal: Mapped[str] = mapped_column(Text, default="")
    # JSON list of the most recent probe results (ProbeResult.as_dict()).
    probes: Mapped[str] = mapped_column(Text, default="[]")
    # service_restarts rows this lease wrote: the ATTEMPT (refused/authorized/
    # aborted) and, for an authorized one, the OUTCOME.
    attempt_restart_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    outcome_restart_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    cleared_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
