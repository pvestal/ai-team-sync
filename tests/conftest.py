"""Test fixtures for ai-team-sync."""

from __future__ import annotations

import os

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

# Force SQLite test database before any imports
os.environ["DATABASE_URL"] = "sqlite+aiosqlite://"

# No test may reach the live Echo Brain (#3537). Every session a test completed
# -- in this process or in a spawned ats-server, which inherits this environment
# -- used to be POSTed to the production default localhost:8309. The URL is set
# here, before any import reads it, to a local tripwire that refuses and records;
# delivery is off unless a test opts in; and a completion event reaching the
# tripwire fails the test that caused it (or the run, if it arrives late).
from .echo_tripwire import Tripwire  # noqa: E402

ECHO_TRIPWIRE = Tripwire()
os.environ["ECHO_BRAIN_URL"] = ECHO_TRIPWIRE.url
os.environ["ATS_EMIT_COMPLETION"] = "0"

from ai_team_sync.models import Base
from ai_team_sync.server import create_app
from ai_team_sync.database import get_db


@pytest.fixture
def echo_tripwire():
    """The run's Echo stand-in, for tests that assert what reached it."""
    return ECHO_TRIPWIRE


@pytest.fixture(autouse=True)
def _no_completion_event_reaches_echo(request):
    start = ECHO_TRIPWIRE.count()
    yield
    leaked = ECHO_TRIPWIRE.completion_events(since=start)
    if "echo_tripwire" in request.fixturenames:
        config = request.config
        config._echo_tripwire_allowed = getattr(config, "_echo_tripwire_allowed", 0) + len(leaked)
        return
    assert not leaked, ("test sent completion event(s) toward Echo: "
                        + Tripwire.describe(leaked))


def pytest_sessionfinish(session, exitstatus):
    """A spawned server can deliver after its test returned; nothing escapes."""
    allowed = getattr(session.config, "_echo_tripwire_allowed", 0)
    leaked = ECHO_TRIPWIRE.completion_events()
    if len(leaked) > allowed:
        session.exitstatus = 1
        print(f"\nECHO TRIPWIRE: {len(leaked)} completion event(s) reached the Echo "
              f"stand-in ({allowed} expected by opted-in tests): {Tripwire.describe(leaked)}")


@pytest.fixture(autouse=True)
def _identified_local_caller(monkeypatch):
    """Every real ATS client reaches it over loopback TCP, where the kernel names
    the caller's OS account (#2741). The in-process ASGI transport has no socket,
    so tests get the answer production gets: this process's own uid. Tests about
    caller identity override this per request."""
    from ai_team_sync import peer_identity

    monkeypatch.setattr(peer_identity, "peer_uid_for_request", lambda request: os.getuid())


@pytest.fixture(autouse=True)
def _no_workstation_governance(monkeypatch, tmp_path_factory):
    """The workstation's ~/.config/ai-team-sync/operator.toml must not govern
    test repo roots such as /opt/anime-studio (#3581 runs git there). Tests that
    need governance set ATS_OPERATOR_CONFIG or ATS_COORDINATED_REPOS themselves."""
    monkeypatch.setenv("ATS_OPERATOR_CONFIG",
                       str(tmp_path_factory.getbasetemp() / "no-operator.toml"))
    monkeypatch.delenv("ATS_COORDINATED_REPOS", raising=False)


@pytest_asyncio.fixture
async def db_engine():
    engine = create_async_engine("sqlite+aiosqlite://", echo=False)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield engine
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
    await engine.dispose()


@pytest_asyncio.fixture
async def db_session(db_engine):
    session_factory = async_sessionmaker(db_engine, class_=AsyncSession, expire_on_commit=False)
    async with session_factory() as session:
        yield session


@pytest_asyncio.fixture
async def client(db_engine):
    app = create_app()

    # Override the database dependency to use test database
    session_factory = async_sessionmaker(db_engine, class_=AsyncSession, expire_on_commit=False)

    async def override_get_db():
        async with session_factory() as session:
            yield session

    app.dependency_overrides[get_db] = override_get_db

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        yield ac
