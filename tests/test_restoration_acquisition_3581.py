"""#3581: lane restoration is an acquisition, serialized with POST /api/locks.

Codex confirmation round on c23f5de: the reaped session's heartbeat ran its
journal CAS, conflict check and lock insert OUTSIDE acquisition_guard, so on
PostgreSQL a restoring heartbeat and an active session's POST /api/locks for the
same governed file could both see no overlap and both commit (SQLite was only
incidentally serialized by the CAS UPDATE's writer lock).

Each round races the two requests for one file and requires exactly one live
owner: either the restoration won (B gets 409) or B won (the lane is reported
not restored, newer_holder). Covered on the in-process app (SQLite), on
PostgreSQL with the in-process lock disabled so only pg_advisory_xact_lock can
exclude, and across separate uvicorn processes on one SQLite file and on one
PostgreSQL database. PostgreSQL runs when ATS_TEST_POSTGRES_URL names a server
on which a throwaway database may be created and dropped.
"""
from __future__ import annotations

import asyncio
import os
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

import httpx
import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import text, update
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

import ai_team_sync
from ai_team_sync.background_tasks import auto_complete_stale_sessions
from ai_team_sync.config import settings
from ai_team_sync.database import get_db
from ai_team_sync.models import Base, ScopeLock, Session
from ai_team_sync.routers import locks as locks_router
from ai_team_sync.server import create_app
from tests.test_ownership_enforcement_3581 import _git, _govern, _make_repo
from tests.test_ownership_hardening_3581 import _free_port, _serve, _wait_healthy

POSTGRES_URL = os.environ.get("ATS_TEST_POSTGRES_URL")
needs_postgres = pytest.mark.skipif(not POSTGRES_URL, reason="ATS_TEST_POSTGRES_URL is not configured")
ROUNDS = 6
FILES = tuple(f"pkg/f{i}.py" for i in range(ROUNDS)) + ("README.md",)


def _long_ago():
    return datetime.now(timezone.utc) - timedelta(hours=settings.session_inactivity_hours + 24)


async def _reap_holder(factory, sid: str) -> None:
    """Age `sid` and its locks past every window, then run the real reaper."""
    async with factory() as db:
        await db.execute(update(Session).where(Session.id == sid).values(
            started_at=_long_ago(), last_heartbeat=_long_ago()))
        await db.execute(update(ScopeLock).where(ScopeLock.session_id == sid).values(
            created_at=_long_ago()))
        await db.commit()
        assert await auto_complete_stale_sessions(db) >= 1
    async with factory() as db:
        row = await db.get(Session, sid)
        assert row.status == "completed" and row.reaped_locks, "the reaper must journal the lane"


async def _race(http, holder: str, rival: str, pattern: str, *, lock_first: bool,
                hb=None, lk=None):
    """Fire the restoring heartbeat and the rival's lock together; return
    (heartbeat response, lock response, live owners of `pattern`). `lock_first`
    schedules the rival's request first, so both orders are exercised."""
    hb_client, lk_client = hb or http, lk or http

    async def beat():
        if lock_first:
            await asyncio.sleep(0.1)  # the rival is inside its held guard by now
        return await hb_client.post(f"/api/sessions/{holder}/heartbeat")

    heartbeat, lock = await asyncio.gather(beat(), lk_client.post(
        "/api/locks", json={"session_id": rival, "pattern": pattern, "mode": "advisory"}))
    owners = [lk["session_id"] for lk in (await http.get("/api/locks")).json()
              if lk["pattern"] == pattern]
    return heartbeat, lock, owners


def _assert_one_owner(heartbeat, lock, owners, holder, rival, pattern):
    assert heartbeat.status_code == 200, heartbeat.text
    assert lock.status_code in (201, 409), lock.text
    assert len(owners) == 1, f"two owners of {pattern}: {owners}"
    body = heartbeat.json()
    if lock.status_code == 201:
        assert owners == [rival]
        assert body["locks_not_restored"] == [pattern]
        assert body["not_restored_reasons"] == ["newer_holder"]
    else:
        assert owners == [holder]
        assert body["locks_restored"] == [pattern]


async def _rounds(http, factory, root, *, hb=None, lk=None) -> list[str]:
    winners = []
    for i, pattern in enumerate(FILES[:ROUNDS]):
        holder = (await http.post("/api/sessions", json={
            "developer": "p", "agent": f"claude-code:{uuid.uuid4().hex[:8]}",
            "scope": [pattern], "auto_lock": True, "repo_root": root})).json()["id"]
        await _reap_holder(factory, holder)
        rival = (await http.post("/api/sessions", json={
            "developer": "p", "agent": f"claude-code:{uuid.uuid4().hex[:8]}",
            "scope": [], "repo_root": root})).json()["id"]
        heartbeat, lock, owners = await _race(http, holder, rival, pattern,
                                              lock_first=bool(i % 2), hb=hb, lk=lk)
        _assert_one_owner(heartbeat, lock, owners, holder, rival, pattern)
        winners.append("restoration" if lock.status_code == 409 else "lock")
    return winners


def _both_orders_won(winners: list[str]) -> None:
    """The race was real: each request won at least one round."""
    assert set(winners) == {"restoration", "lock"}, winners


@pytest.fixture
def repo(tmp_path, monkeypatch):
    root = _make_repo(tmp_path / "repo", FILES)
    _govern(monkeypatch, tmp_path, root)
    return root


@pytest.fixture
def held_window(monkeypatch):
    """Hold every acquisition 150 ms between its conflict check and its insert,
    so the other request always arrives while the first is mid-acquisition:
    real contention in both orders, and the widest window for a double grant."""
    from ai_team_sync.routers import sessions as sessions_router

    checked = sessions_router._check_scope_conflicts

    async def slow(*args, **kwargs):
        conflicts = await checked(*args, **kwargs)
        await asyncio.sleep(0.15)
        return conflicts

    monkeypatch.setattr(sessions_router, "_check_scope_conflicts", slow)


# ── one process, SQLite ──────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_restoration_and_a_rival_lock_never_both_own_the_file(
        client, db_engine, repo, held_window):
    factory = async_sessionmaker(db_engine, class_=AsyncSession, expire_on_commit=False)
    _both_orders_won(await _rounds(client, factory, repo))


# ── PostgreSQL: a throwaway database per test ────────────────────────────

async def _postgres_database():
    admin = create_async_engine(POSTGRES_URL, isolation_level="AUTOCOMMIT")
    name = f"ats_3581_{uuid.uuid4().hex[:12]}"
    async with admin.connect() as conn:
        await conn.execute(text(f'CREATE DATABASE "{name}"'))
    await admin.dispose()
    return name, make_url(POSTGRES_URL).set(database=name).render_as_string(hide_password=False)


async def _drop_postgres_database(name: str) -> None:
    admin = create_async_engine(POSTGRES_URL, isolation_level="AUTOCOMMIT")
    async with admin.connect() as conn:
        await conn.execute(text(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)'))
    await admin.dispose()


class _Unshared(dict):
    """No in-process acquisition lock: every request gets its own, so only the
    database's lock can exclude."""

    def setdefault(self, key, default=None):
        return asyncio.Lock()


@pytest.fixture
async def pg_client(monkeypatch):
    name, url = await _postgres_database()
    engine = create_async_engine(url)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    app = create_app()

    async def override_get_db():
        async with factory() as s:
            yield s

    app.dependency_overrides[get_db] = override_get_db
    monkeypatch.setattr(locks_router, "_ACQUISITION_LOCKS", _Unshared())
    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
            yield c, factory
    finally:
        await engine.dispose()
        await _drop_postgres_database(name)


@needs_postgres
@pytest.mark.asyncio
async def test_postgres_advisory_lock_alone_serializes_restoration(pg_client, repo, held_window):
    http, factory = pg_client
    _both_orders_won(await _rounds(http, factory, repo))


@needs_postgres
@pytest.mark.asyncio
async def test_postgres_advisory_lock_alone_serializes_rival_locks(pg_client, repo):
    http, _factory = pg_client
    sids = [(await http.post("/api/sessions", json={
        "developer": "p", "agent": f"claude-code:{i:08d}", "scope": [],
        "repo_root": repo})).json()["id"] for i in range(8)]
    results = await asyncio.gather(*[
        http.post("/api/locks", json={"session_id": sid, "pattern": "README.md",
                                     "mode": "advisory"}) for sid in sids])
    assert sorted(r.status_code for r in results) == [201] + [409] * 7, \
        [r.text for r in results]


# ── separate server processes ────────────────────────────────────────────

async def _multi_process(tmp_path, root, database_url, async_url):
    config = tmp_path / "operator.toml"
    config.write_text(f'[governance]\nrepositories = ["{root}"]\n')
    env = {**os.environ, "DATABASE_URL": database_url, "ATS_OPERATOR_CONFIG": str(config),
           "PYTHONPATH": str(Path(ai_team_sync.__file__).resolve().parent.parent),
           "SLACK_WEBHOOK_URL": "", "TELEGRAM_BOT_TOKEN": "", "TELEGRAM_CHAT_ID": ""}
    env.pop("ATS_COORDINATED_REPOS", None)
    ports = [_free_port(), _free_port()]
    procs = [_serve(ports[0], env, tmp_path)]
    try:
        await asyncio.to_thread(_wait_healthy, ports[0], procs[0])  # creates the schema
        procs.append(_serve(ports[1], env, tmp_path))
        await asyncio.to_thread(_wait_healthy, ports[1], procs[1])
        engine = create_async_engine(async_url)
        factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
        try:
            async with httpx.AsyncClient(base_url=f"http://127.0.0.1:{ports[0]}", timeout=30) as a, \
                    httpx.AsyncClient(base_url=f"http://127.0.0.1:{ports[1]}", timeout=30) as b:
                return await _rounds(a, factory, root, hb=a, lk=b)
        finally:
            await engine.dispose()
    finally:
        for proc in procs:
            proc.terminate()
        for proc in procs:
            proc.wait(timeout=10)


@pytest.mark.asyncio
async def test_separate_processes_on_one_sqlite_file(tmp_path):
    root = _make_repo(tmp_path / "repo", FILES)
    url = f"sqlite+aiosqlite:///{tmp_path / 'ats.db'}"
    await _multi_process(tmp_path, root, url, url)


@needs_postgres
@pytest.mark.asyncio
async def test_separate_processes_on_one_postgres_database(tmp_path):
    root = _make_repo(tmp_path / "repo", FILES)
    name, url = await _postgres_database()
    try:
        await _multi_process(tmp_path, root, url, url)
    finally:
        await _drop_postgres_database(name)
