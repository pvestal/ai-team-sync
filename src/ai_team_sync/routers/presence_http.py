"""HTTP presence endpoint — hook-driven auto-emit (slice 2 of agent file-awareness).

The WebSocket path (`/ws/presence`) is for the live UI: it holds a connection and
removes you on disconnect. A git/agent hook is a short-lived process that can't hold a
socket, so it POSTs here instead. Presence carries a TTL (presence.STALE_SECONDS), so
each edit acts as a heartbeat: "actively editing right now"; it ages out when edits stop.
"""
from __future__ import annotations

from fastapi import APIRouter

from ai_team_sync.presence import store
from ai_team_sync.schemas import PresenceEntry, PresenceUpdate

router = APIRouter(prefix="/presence", tags=["presence"])


@router.post("", response_model=list[PresenceEntry])
async def update_presence(body: PresenceUpdate):
    """Set/refresh a developer's live presence (files + one-line intent)."""
    store.update(body.developer, body.agent, body.files, body.intent)
    await store.broadcast()
    return store.get_all()


@router.get("", response_model=list[PresenceEntry])
async def list_presence():
    """Who is actively editing right now (non-stale presence)."""
    return store.get_all()
