"""Existing claimed ticket rows must not be mistaken for direct messages."""

from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from ai_team_sync import database
from ai_team_sync.models import AgentMessage, Base, Session


@pytest.mark.asyncio
async def test_existing_message_rows_backfill_addressing_mode(monkeypatch):
    engine = create_async_engine("sqlite+aiosqlite://")
    now = datetime.now(timezone.utc)
    try:
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        factory = async_sessionmaker(engine, expire_on_commit=False)
        async with factory() as db:
            old = Session(developer="p", agent="claude-code", started_at=now)
            later = Session(developer="p", agent="codex", started_at=now)
            db.add_all([old, later])
            await db.flush()
            direct = AgentMessage(
                sender_session_id=later.id, recipient_session_id=old.id,
                sender_agent="codex", sender_developer="p",
                recipient_agent="claude-code", body="direct",
                created_at=now + timedelta(seconds=1))
            ticket = AgentMessage(
                sender_session_id=old.id, recipient_session_id=later.id,
                ticket_id=2907, sender_agent="claude-code", sender_developer="p",
                recipient_agent="codex", body="ticket",
                created_at=now - timedelta(seconds=1))
            db.add_all([direct, ticket])
            await db.commit()
            direct_id, ticket_id, old_id = direct.id, ticket.id, old.id

        # Recreate the deployed a66e0d4 message columns on an existing DB.
        async with engine.begin() as conn:
            await conn.execute(text("ALTER TABLE agent_messages DROP COLUMN delivery_history"))
            await conn.execute(text("ALTER TABLE agent_messages DROP COLUMN addressing_mode"))
            await conn.execute(text(
                "ALTER TABLE agent_messages DROP COLUMN original_recipient_session_id"))
        monkeypatch.setattr(database, "engine", engine)
        await database.init_db()

        async with engine.connect() as conn:
            rows = (await conn.execute(text("""
                SELECT id, addressing_mode, original_recipient_session_id
                FROM agent_messages WHERE id IN (:direct_id, :ticket_id)
            """), {"direct_id": direct_id, "ticket_id": ticket_id})).all()
        by_id = {row.id: row for row in rows}
        assert by_id[direct_id].addressing_mode == "session"
        assert by_id[direct_id].original_recipient_session_id == old_id
        assert by_id[ticket_id].addressing_mode == "ticket"
        assert by_id[ticket_id].original_recipient_session_id is None
    finally:
        await engine.dispose()
