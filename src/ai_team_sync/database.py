"""Database engine and session factory. SQLite by default, Postgres with asyncpg."""

from __future__ import annotations

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from ai_team_sync.config import settings


# Idempotent lightweight column additions for existing DBs (init_db uses create_all,
# which creates missing TABLES but never alters existing ones). Each entry is applied
# inside a savepoint so a re-run / already-present column is a harmless no-op even
# on PostgreSQL, where one failed statement otherwise aborts the whole transaction.
# Keep these append-only and backwards-compatible (new nullable/defaulted columns only).
_COLUMN_MIGRATIONS = [
    ("scope_locks", "reason", "TEXT DEFAULT ''"),
    ("sessions", "last_heartbeat", "TIMESTAMP"),  # nullable liveness signal (Gap 1)
    ("sessions", "repo_root", "TEXT DEFAULT ''"),  # repo anchoring (ats-lockcheck-repo-anchoring-p01)
    # Reaper-vs-operator completion. Existing rows backfill to 0 (= operator),
    # which is the conservative direction: a historical auto-completion will not
    # resurrect, it just behaves as it does today.
    ("sessions", "auto_completed", "BOOLEAN DEFAULT 0"),
    # Truthful delegation provenance. Historical rows backfill to '', which
    # reads correctly as "this record cannot evidence which worker ran" rather
    # than silently asserting the requested one did.
    ("delegations", "resolved_binary", "TEXT DEFAULT ''"),
    ("delegations", "launch_spec_version", "TEXT DEFAULT ''"),
    # Caller identity (#2741). Existing sessions backfill as unidentified and
    # unbound, and existing locks as non-bearing: nothing that predates the
    # binding can be read as a grant.
    ("sessions", "creator_uid", "INTEGER"),
    ("sessions", "approval_token_hash", "VARCHAR(64) DEFAULT ''"),
    ("sessions", "bound_worker", "TEXT DEFAULT ''"),
    ("sessions", "bound_uid", "INTEGER"),
    ("sessions", "task_id", "INTEGER"),
    ("sessions", "delegation_id", "TEXT"),
    ("scope_locks", "authority_bearing", "BOOLEAN DEFAULT 0"),
    # What the reaper took, so resurrection can give it back (#2760). Historical
    # rows backfill to '', which reads correctly as "nothing was journalled" —
    # a session reaped before this shipped resurrects exactly as it does today.
    ("sessions", "reaped_locks", "TEXT DEFAULT ''"),
    # Which lanes resurrection refused, so the guard and the board can disagree
    # with a stale `scope` (#2760). Backfills to '' — a session that never lost a
    # lane reads exactly as it does today.
    ("sessions", "locks_not_restored", "TEXT DEFAULT ''"),
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
                async with conn.begin_nested():
                    await conn.execute(text(f"ALTER TABLE {table} ADD COLUMN {column} {coldef}"))
            except Exception:
                pass  # column already exists (or DB doesn't support it) — harmless
