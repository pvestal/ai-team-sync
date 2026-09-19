"""Assignment history and ticket-mailbox turnover for one logical message."""

from __future__ import annotations

import json
from datetime import datetime, timezone

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from ai_team_sync.models import AgentMessage, Handoff


def append_delivery_event(message: AgentMessage, action: str, session_id: str,
                          reason: str, *, at: datetime | None = None) -> None:
    history = json.loads(message.delivery_history or "[]")
    history.append({
        "action": action,
        "recipient_session_id": session_id,
        "reason": reason,
        "at": (at or datetime.now(timezone.utc)).isoformat(),
    })
    message.delivery_history = json.dumps(history)


async def release_unread_ticket_messages(db: AsyncSession, session_id: str) -> int:
    """Return unread ticket claims to the mailbox when their owner ends.

    The compare-and-set against recipient and receipt serializes this with an
    acknowledgement. Acknowledged messages stay assigned to their one reader.
    Direct messages remain addressed to their exact session until their sender
    explicitly readdresses them.
    """
    rows = (await db.execute(select(AgentMessage).where(
        AgentMessage.recipient_session_id == session_id,
        AgentMessage.addressing_mode == "ticket",
        AgentMessage.acknowledged_at.is_(None),
    ))).scalars().all()
    released = 0
    for row in rows:
        result = await db.execute(update(AgentMessage).where(
            AgentMessage.id == row.id,
            AgentMessage.recipient_session_id == session_id,
            AgentMessage.acknowledged_at.is_(None),
        ).values(recipient_session_id=None, recipient_agent="")
                  .execution_options(synchronize_session=False))
        if not result.rowcount:
            continue
        if not json.loads(row.delivery_history or "[]"):
            append_delivery_event(row, "assigned", session_id,
                                  "legacy_ticket_claim_time_unknown")
        append_delivery_event(row, "released", session_id, "recipient_completed")
        row.recipient_session_id = None
        row.recipient_agent = ""
        if row.handoff_id:
            handoff = await db.get(Handoff, row.handoff_id)
            if handoff is not None:
                handoff.recipient_session_id = None
        released += 1
    return released
