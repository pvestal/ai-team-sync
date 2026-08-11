"""Liveness from ordinary traffic — a session proves it is alive by being used.

WHY THIS EXISTS. The only writer of `last_heartbeat` used to be the dedicated
/heartbeat endpoint, driven by a per-turn Stop hook, and the presence/lockcheck
hooks fire only on Edit|Write|MultiEdit. An agent that spends twenty minutes
reading files, running commands and querying databases therefore emits NOTHING,
and the fast reaper (session_heartbeat_timeout_minutes, default 20) completes it
mid-work. Measured live 2026-08-10: session bc62c5e9 started 03:17:32 and was
auto-completed at 03:39:55 with "silent >20m (heartbeat lost)" while its agent
was continuously making tool calls. The sessions doing the longest uninterrupted
work were the most likely to be declared dead.

config.py's own note — "keep comfortably above the client heartbeat cadence (a
per-turn Stop hook)" — assumes a turn is short. A deep-work turn is unbounded, so
no fixed window can be chosen safely as long as liveness depends on turn
boundaries. The fix is to stop inferring liveness from turns: ANY request that
identifies its session refreshes it.

DELIBERATELY ACTIVE-ONLY. touch_session_liveness never revives a completed
session. Resurrection is a decision that belongs to the /heartbeat endpoint,
where the operator-completed vs reaper-completed distinction is enforced;
allowing a passing header to reopen finished work would make every stray request
a resurrection.

FAIL-OPEN. Coordination must never wedge real work (the same doctrine as the
lockcheck hook), so every failure here is swallowed and logged at debug.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone

from fastapi import Depends, Request
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ai_team_sync.database import get_db
from ai_team_sync.models import Session

logger = logging.getLogger(__name__)

# A client identifies itself with EITHER header. The session id is exact; the
# agent label is what the presence hook and other short-lived hooks already
# carry, so they can prove liveness without first resolving an id.
SESSION_HEADER = "X-ATS-Session-Id"
AGENT_HEADER = "X-ATS-Agent"


async def touch_session_liveness(
    db: AsyncSession,
    *,
    session_id: str | None = None,
    agent: str | None = None,
) -> bool:
    """Refresh last_heartbeat for the matching ACTIVE session. True if bumped.

    Resolution order: exact session id, then newest active session for `agent`.
    An unknown id/agent is a no-op, not an error — a stale pointer is normal
    after a reap and must not fail the caller's real request.
    """
    if not session_id and not agent:
        return False
    try:
        stmt = select(Session).where(Session.status == "active")
        if session_id:
            stmt = stmt.where(Session.id == session_id)
        else:
            stmt = stmt.where(Session.agent == agent).order_by(Session.started_at.desc())
        session = (await db.execute(stmt.limit(1))).scalar_one_or_none()
        if session is None:
            return False
        session.last_heartbeat = datetime.now(timezone.utc)
        await db.commit()
        return True
    except Exception:  # noqa: BLE001 — liveness must never break a request
        logger.debug("liveness touch failed", exc_info=True)
        return False


async def liveness_from_request(
    request: Request, db: AsyncSession = Depends(get_db)
) -> None:
    """App-wide dependency: any request carrying a session header stays alive.

    Registered as a FastAPI global dependency rather than ASGI middleware on
    purpose — middleware bypasses dependency_overrides, so it would talk to the
    production engine even under test, and the behaviour would be untestable.
    """
    sid = request.headers.get(SESSION_HEADER)
    agent = request.headers.get(AGENT_HEADER)
    if not sid and not agent:
        return
    await touch_session_liveness(db, session_id=sid, agent=agent)
