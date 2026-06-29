"""Database engine and session factory. SQLite by default, Postgres with asyncpg."""

from __future__ import annotations

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from ai_team_sync.config import settings


# Idempotent lightweight column additions for existing DBs (init_db uses create_all,
# which creates missing TABLES but never alters existing ones). Each entry is applied
# inside try/except so a re-run / already-present column is a harmless no-op. Keep
# these append-only and backwards-compatible (new nullable/defaulted columns only).
_COLUMN_MIGRATIONS = [
    ("scope_locks", "reason", "TEXT DEFAULT ''"),
    ("sessions", "last_heartbeat", "TIMESTAMP"),  # nullable liveness signal (Gap 1)
]

engine = create_async_engine(
    settings.database_url,
    echo=False,
    # SQLite needs this for async
    **({} if "postgresql" in settings.database_url else {"connect_args": {"check_same_thread": False}}),
)

async_session = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)


async def get_db():
    """FastAPI dependency that yields an async database session."""
    async with async_session() as session:
        yield session


async def init_db():
    """Create all tables, then apply idempotent column additions for existing DBs."""
    from ai_team_sync.models import Base

    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
        for table, column, coldef in _COLUMN_MIGRATIONS:
            try:
                await conn.execute(text(f"ALTER TABLE {table} ADD COLUMN {column} {coldef}"))
            except Exception:
                pass  # column already exists (or DB doesn't support it) — harmless
