"""PostgreSQL regression coverage for additive column migrations (#2764)."""

from __future__ import annotations

import os
import uuid

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from ai_team_sync import database
from ai_team_sync.models import Base


POSTGRES_URL = os.environ.get("ATS_TEST_POSTGRES_URL")


@pytest.mark.skipif(not POSTGRES_URL, reason="ATS_TEST_POSTGRES_URL is not configured")
async def test_existing_postgres_schema_applies_later_column_migrations(monkeypatch):
    """An already-present column must not poison later ALTER statements."""
    admin_engine = create_async_engine(POSTGRES_URL)
    schema = f"ats_migration_2764_{uuid.uuid4().hex}"
    schema_engine = None

    try:
        async with admin_engine.begin() as conn:
            await conn.execute(text(f"CREATE SCHEMA {schema}"))

        schema_engine = create_async_engine(
            POSTGRES_URL,
            connect_args={"server_settings": {"search_path": schema}},
        )
        monkeypatch.setattr(database, "engine", schema_engine)

        # Model an existing installation: all earlier columns are present, while
        # the two additions from #2760 have not been applied yet.
        async with schema_engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
            await conn.execute(text("ALTER TABLE sessions DROP COLUMN reaped_locks"))
            await conn.execute(text("ALTER TABLE sessions DROP COLUMN locks_not_restored"))

        await database.init_db()

        async with schema_engine.connect() as conn:
            result = await conn.execute(text("""
                SELECT column_name, data_type, is_nullable, column_default
                FROM information_schema.columns
                WHERE table_schema = current_schema()
                  AND table_name = 'sessions'
                  AND column_name IN ('reaped_locks', 'locks_not_restored')
                ORDER BY column_name
            """))
            columns = [tuple(row) for row in result]

        assert columns == [
            ("locks_not_restored", "text", "YES", "''::text"),
            ("reaped_locks", "text", "YES", "''::text"),
        ]
    finally:
        if schema_engine is not None:
            await schema_engine.dispose()
        async with admin_engine.begin() as conn:
            await conn.execute(text(f"DROP SCHEMA IF EXISTS {schema} CASCADE"))
        await admin_engine.dispose()
