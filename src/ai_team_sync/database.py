"""Database engine and session factory. SQLite by default, Postgres with asyncpg."""

from __future__ import annotations

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from ai_team_sync.config import settings

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
    """Create all tables."""
    from ai_team_sync.models import Base

    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
