"""Lifecycle projections must use installed composite indexes."""

from __future__ import annotations

from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from ai_team_sync import database
from ai_team_sync.models import Base


async def test_init_db_restores_observability_indexes_on_existing_database(monkeypatch):
    engine = create_async_engine("sqlite+aiosqlite://")
    try:
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
            await conn.execute(text("DROP INDEX ix_file_activities_created_at_id"))
            missing = await conn.execute(
                text(
                    "SELECT name FROM sqlite_master WHERE type='index' "
                    "AND name='ix_file_activities_created_at_id'"
                )
            )
            assert missing.scalar_one_or_none() is None

        monkeypatch.setattr(database, "engine", engine)
        await database.init_db()

        async with engine.connect() as conn:
            restored = await conn.execute(
                text(
                    "SELECT name FROM sqlite_master WHERE type='index' "
                    "AND name='ix_file_activities_created_at_id'"
                )
            )
            assert restored.scalar_one() == "ix_file_activities_created_at_id"
            columns = await conn.execute(
                text("PRAGMA index_info(ix_file_activities_created_at_id)")
            )
            assert [row.name for row in columns] == ["created_at", "id"]
            plan = await conn.execute(
                text(
                    "EXPLAIN QUERY PLAN SELECT * FROM file_activities "
                    "WHERE created_at >= :cutoff "
                    "ORDER BY created_at DESC, id DESC LIMIT 101"
                ),
                {"cutoff": "2026-10-01T00:00:00+00:00"},
            )
            plan_text = " ".join(str(row.detail) for row in plan)
            assert "ix_file_activities_created_at_id" in plan_text
            assert "TEMP B-TREE" not in plan_text
    finally:
        await engine.dispose()
