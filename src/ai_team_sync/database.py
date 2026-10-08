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
    (
        "sessions",
        "repo_root",
        "TEXT DEFAULT ''",
    ),  # repo anchoring (ats-lockcheck-repo-anchoring-p01)
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
    ("sessions", "ticket_id", "INTEGER"),
    ("decisions", "ticket_id", "INTEGER"),
    ("decisions", "recipient_session_id", "VARCHAR(36)"),
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
    # Existing direct vs ticket rows can be classified by their chronology:
    # a ticket claim is made only by a session created AFTER the message.
    # The one-time backfill below preserves that distinction for already-claimed
    # ticket messages. New writers always set addressing_mode explicitly.
    ("agent_messages", "original_recipient_session_id", "VARCHAR(36)"),
    ("agent_messages", "addressing_mode", "VARCHAR(20) DEFAULT 'legacy'"),
    ("agent_messages", "delivery_history", "TEXT DEFAULT '[]'"),
]

# Existing databases need the composite lifecycle indexes declared in models.py;
# create_all only creates them for new tables. These are projections indexes,
# not a new event store or source of truth.
_INDEX_MIGRATIONS = [
    ("ix_sessions_started_at_id", "sessions", "started_at, id"),
    ("ix_sessions_completed_at_id", "sessions", "completed_at, id"),
    ("ix_agent_messages_created_at_id", "agent_messages", "created_at, id"),
    ("ix_agent_messages_acknowledged_at_id", "agent_messages", "acknowledged_at, id"),
    ("ix_handoffs_created_at_id", "handoffs", "created_at, id"),
    ("ix_delegations_created_at_id", "delegations", "created_at, id"),
    ("ix_delegations_closed_at_id", "delegations", "closed_at, id"),
    (
        "ix_delegations_parent_session_created_at",
        "delegations",
        "parent_session_id, created_at",
    ),
    (
        "ix_delegations_child_session_created_at",
        "delegations",
        "child_session_id, created_at",
    ),
    ("ix_decisions_created_at_id", "decisions", "created_at, id"),
    ("ix_authority_checks_created_at_id", "authority_checks", "created_at, id"),
    ("ix_file_activities_created_at_id", "file_activities", "created_at, id"),
    ("ix_override_requests_created_at_id", "override_requests", "created_at, id"),
    ("ix_override_requests_responded_at_id", "override_requests", "responded_at, id"),
    ("ix_service_restarts_created_at_id", "service_restarts", "created_at, id"),
    ("ix_commit_records_created_at_id", "commit_records", "created_at, id"),
]

engine = create_async_engine(
    settings.database_url,
    echo=False,
    # SQLite needs this for async
    **(
        {}
        if "postgresql" in settings.database_url
        else {"connect_args": {"check_same_thread": False}}
    ),
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
        # Classify historical rows only once. A direct send could not predate
        # its recipient session; a deferred ticket claim always does. If the
        # recipient row is gone, keep the row as legacy instead of guessing.
        await conn.execute(text("""
            UPDATE agent_messages SET addressing_mode = 'ticket'
            WHERE addressing_mode = 'legacy' AND ticket_id IS NOT NULL AND (
                recipient_session_id IS NULL OR created_at <= (
                    SELECT started_at FROM sessions
                    WHERE sessions.id = agent_messages.recipient_session_id))
        """))
        await conn.execute(text("""
            UPDATE agent_messages SET addressing_mode = 'session',
                original_recipient_session_id = recipient_session_id
            WHERE addressing_mode = 'legacy' AND recipient_session_id IS NOT NULL
              AND EXISTS (SELECT 1 FROM sessions
                          WHERE sessions.id = agent_messages.recipient_session_id)
        """))

    # PostgreSQL's ordinary CREATE INDEX holds a writer-blocking lock until the
    # surrounding startup transaction commits. Build these read-side indexes
    # concurrently and in AUTOCOMMIT instead; SQLite has no concurrent form.
    if engine.dialect.name == "postgresql":
        async with engine.connect() as raw_conn:
            conn = await raw_conn.execution_options(isolation_level="AUTOCOMMIT")
            for name, table, columns in _INDEX_MIGRATIONS:
                await conn.execute(
                    text(
                        f"CREATE INDEX CONCURRENTLY IF NOT EXISTS {name} " f"ON {table} ({columns})"
                    )
                )
    else:
        async with engine.begin() as conn:
            for name, table, columns in _INDEX_MIGRATIONS:
                await conn.execute(
                    text(f"CREATE INDEX IF NOT EXISTS {name} ON {table} ({columns})")
                )
