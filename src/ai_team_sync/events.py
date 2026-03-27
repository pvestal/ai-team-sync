"""Event broadcasting system for WebSocket notifications."""

from __future__ import annotations

import asyncio
from typing import Any
from collections import defaultdict

# Store WebSocket connections by session ID
_websocket_connections: dict[str, list[asyncio.Queue]] = defaultdict(list)


async def subscribe_to_session(session_id: str) -> asyncio.Queue:
    """Subscribe to events for a session. Returns a queue that receives events."""
    queue: asyncio.Queue = asyncio.Queue()
    _websocket_connections[session_id].append(queue)
    return queue


async def unsubscribe_from_session(session_id: str, queue: asyncio.Queue):
    """Unsubscribe from session events."""
    if session_id in _websocket_connections:
        try:
            _websocket_connections[session_id].remove(queue)
        except ValueError:
            pass

        # Clean up empty lists
        if not _websocket_connections[session_id]:
            del _websocket_connections[session_id]


async def broadcast_event(session_id: str, event_type: str, data: dict[str, Any]):
    """Broadcast an event to all subscribers of a session."""
    if session_id not in _websocket_connections:
        return

    event = {
        "type": event_type,
        "session_id": session_id,
        "data": data,
    }

    # Send to all connected clients for this session
    dead_queues = []
    for queue in _websocket_connections[session_id]:
        try:
            await asyncio.wait_for(queue.put(event), timeout=1.0)
        except (asyncio.TimeoutError, Exception):
            dead_queues.append(queue)

    # Clean up dead connections
    for queue in dead_queues:
        try:
            _websocket_connections[session_id].remove(queue)
        except ValueError:
            pass
