"""HTTP presence endpoint — hook-driven auto-emit (slice 2 of agent file-awareness).

The WebSocket path (`/ws/presence`) is for the live UI: it holds a connection and
removes you on disconnect. A git/agent hook is a short-lived process that can't hold a
socket, so it POSTs here instead. Presence carries a TTL (presence.STALE_SECONDS), so
each edit acts as a heartbeat: "actively editing right now"; it ages out when edits stop.
"""
from __future__ import annotations

from fastapi import APIRouter

from ai_team_sync.presence import store
from ai_team_sync.schemas import (
    PresenceEntry,
    PresenceUpdate,
    WhosEditingRequest,
    WhosEditingResult,
)

router = APIRouter(prefix="/presence", tags=["presence"])


def _path_matches(query: str, presence_file: str) -> bool:
    """Match a queried path against a presence file, tolerant of absolute vs
    repo-relative forms (e.g. '/repo/src/x.py' vs 'src/x.py')."""
    a, b = query.strip(), presence_file.strip()
    if not a or not b:
        return False
    return a == b or a.endswith("/" + b) or b.endswith("/" + a)


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


@router.post("/check", response_model=list[WhosEditingResult])
async def whos_editing(body: WhosEditingRequest):
    """For each path, who else is actively editing it right now (+ their intent).

    The consume side of agent file-awareness (slice 3): an agent calls this BEFORE
    editing to self-coordinate ("someone is in this file → pick another / wait").
    Live presence only — declared scope locks are a separate check (/locks/check).
    """
    present = store.get_all()
    ex_agent = (body.exclude_agent or "").strip()

    def _is_me(p: dict) -> bool:
        # Prefer excluding by session (agent label) so a concurrent same-developer
        # session is still surfaced; fall back to developer for legacy callers.
        if ex_agent:
            return p["agent"] == ex_agent
        return p["developer"] == body.exclude_developer

    results = []
    for path in body.paths:
        editors = [
            PresenceEntry(**p)
            for p in present
            if not _is_me(p)
            and any(_path_matches(path, f) for f in p["files"])
        ]
        results.append(WhosEditingResult(path=path, editors=editors))
    return results
