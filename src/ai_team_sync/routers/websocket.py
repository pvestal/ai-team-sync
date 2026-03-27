"""WebSocket endpoints for real-time notifications."""

from __future__ import annotations

import asyncio

from fastapi import APIRouter, WebSocket, WebSocketDisconnect
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ai_team_sync.database import get_db
from ai_team_sync.events import subscribe_to_session, unsubscribe_from_session
from ai_team_sync.models import Session

router = APIRouter(tags=["websocket"])


@router.websocket("/ws/sessions/{session_id}")
async def session_websocket(websocket: WebSocket, session_id: str):
    """
    WebSocket endpoint for real-time session events.

    Events pushed:
    - override.requested - Someone requested override for your lock
    - override.responded - Your override request was approved/denied
    - session.conflict - Overlap detected with your session
    - session.completed - Someone completed a session affecting your scope
    """
    await websocket.accept()

    # Subscribe to events for this session
    queue = await subscribe_to_session(session_id)

    try:
        while True:
            # Wait for events from the queue
            event = await queue.get()

            # Send event to client
            await websocket.send_json(event)

    except WebSocketDisconnect:
        pass
    except Exception as e:
        print(f"WebSocket error: {e}")
    finally:
        # Clean up subscription
        await unsubscribe_from_session(session_id, queue)
        try:
            await websocket.close()
        except:
            pass
