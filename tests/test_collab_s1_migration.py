"""#3585 slice 1: upgrading a populated, pre-roles database.

A live installation has rows written before ticket roles existed, including
several active sessions bound to one ticket (production has them today). The
upgrade must add the new columns as empty, leave every existing row's meaning
alone, build the one-live-owner index on that data, and then enforce it.

SQLite always runs. PostgreSQL runs when ATS_TEST_POSTGRES_URL points at a
disposable database (the same switch as tests/test_database_migrations_2764.py);
there the index is built CONCURRENTLY, as init_db does in production.
"""

from __future__ import annotations

import os
import uuid

import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.pool import NullPool

from ai_team_sync import database, ticket_roles
from ai_team_sync.models import Base

POSTGRES_URL = os.environ.get("ATS_TEST_POSTGRES_URL")
NEW_SESSION_COLUMNS = ("ticket_role",)
NEW_DELEGATION_COLUMNS = ("verdict_code", "effective_verdict", "structured_verdict")


async def _make_legacy(engine):
    """Today's schema minus everything #3585 adds, plus pre-roles rows."""
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
        await conn.execute(text("DROP INDEX IF EXISTS ux_sessions_live_ticket_owner"))
        for column in NEW_SESSION_COLUMNS:
            await conn.execute(text(f"ALTER TABLE sessions DROP COLUMN {column}"))
        for column in NEW_DELEGATION_COLUMNS:
            await conn.execute(text(f"ALTER TABLE delegations DROP COLUMN {column}"))
        for sid in ("legacy-a", "legacy-b"):
            await conn.execute(text(
                "INSERT INTO sessions (id, developer, agent, scope, description, status, branch,"
                " repo_root, started_at, auto_completed, creator_uid, approval_token_hash,"
                " bound_worker, ticket_id, reaped_locks, locks_not_restored) VALUES"
                " (:id, 'pvestal', 'claude-code:x', '[]', '', 'active', '', '', CURRENT_TIMESTAMP,"
                " FALSE, 1000, '', '', 3585, '', '')"), {"id": sid})
        await conn.execute(text(
            "INSERT INTO delegations (id, parent_session_id, parent_task, delegating_worker,"
            " delegated_worker, resolved_binary, launch_spec_version, mode, repo_root, scope,"
            " objective, acceptance, prohibitions, lease_expires_at, state, result_summary,"
            " evidence, verdict, created_at) VALUES ('legacy-d', 'legacy-a', '', 'claude-code',"
            " 'codex', '', '', 'VERIFY', '', '[]', '', 'x', '[]', CURRENT_TIMESTAMP, 'returned',"
            " 'free text BLOCK', '{}', 'owner note', CURRENT_TIMESTAMP)"))


async def _assert_upgraded(engine):
    async with engine.connect() as conn:
        roles = (await conn.execute(text(
            "SELECT id, ticket_role FROM sessions ORDER BY id"))).all()
        assert [tuple(r) for r in roles] == [("legacy-a", None), ("legacy-b", None)]
        delegation = (await conn.execute(text(
            "SELECT verdict, verdict_code, effective_verdict, structured_verdict, result_summary"
            " FROM delegations WHERE id = 'legacy-d'"))).one()
        # The parent's note and the free-text result are untouched; no verdict invented.
        assert tuple(delegation) == ("owner note", "", "", "", "free text BLOCK")

    # The index exists and refuses a second live owner on one ticket and account.
    async with engine.begin() as conn:
        await conn.execute(text(
            "UPDATE sessions SET ticket_role = 'owner' WHERE id = 'legacy-a'"))
    with pytest.raises(IntegrityError) as refused:
        async with engine.begin() as conn:
            await conn.execute(text(
                "UPDATE sessions SET ticket_role = 'owner' WHERE id = 'legacy-b'"))
    assert ticket_roles.is_owner_conflict(refused.value)
    # A completed owner leaves the index, so a successor can own.
    async with engine.begin() as conn:
        await conn.execute(text("UPDATE sessions SET status = 'completed' WHERE id = 'legacy-a'"))
        await conn.execute(text(
            "UPDATE sessions SET ticket_role = 'owner' WHERE id = 'legacy-b'"))


@pytest.mark.asyncio
async def test_a_populated_sqlite_database_upgrades_and_enforces_one_owner(tmp_path, monkeypatch):
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'ats.db'}", poolclass=NullPool)
    monkeypatch.setattr(database, "engine", engine)
    try:
        await _make_legacy(engine)
        await database.init_db()
        await database.init_db()  # idempotent: a restart re-runs it
        await _assert_upgraded(engine)
    finally:
        await engine.dispose()


@pytest.mark.asyncio
@pytest.mark.skipif(not POSTGRES_URL, reason="ATS_TEST_POSTGRES_URL is not configured")
async def test_a_populated_postgres_database_upgrades_and_enforces_one_owner(monkeypatch):
    admin = create_async_engine(POSTGRES_URL)
    schema = f"ats_3585_{uuid.uuid4().hex}"
    engine = None
    try:
        async with admin.begin() as conn:
            await conn.execute(text(f"CREATE SCHEMA {schema}"))
        engine = create_async_engine(
            POSTGRES_URL, connect_args={"server_settings": {"search_path": schema}})
        monkeypatch.setattr(database, "engine", engine)
        await _make_legacy(engine)
        await database.init_db()
        await database.init_db()
        async with engine.connect() as conn:
            index = (await conn.execute(text(
                "SELECT indexdef FROM pg_indexes WHERE schemaname = current_schema()"
                " AND indexname = 'ux_sessions_live_ticket_owner'"))).scalar_one()
        assert "UNIQUE" in index and "ticket_role" in index
        await _assert_upgraded(engine)
    finally:
        if engine is not None:
            await engine.dispose()
        async with admin.begin() as conn:
            await conn.execute(text(f"DROP SCHEMA IF EXISTS {schema} CASCADE"))
        await admin.dispose()
