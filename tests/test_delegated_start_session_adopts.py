"""A delegated child's start_session must adopt its bound row, never add a twin.

Observed 2026-10-09: every delegated Codex run (10 of 10 in 24h) produced two
`codex:delegate` sessions, the one the wrapper bound to the delegation and,
8-17s later, an unbound one the child opened itself by calling start_session
as its ATS-first instructions say. The twin then sent the parent's messages,
so provenance pointed at a session no delegation accounts for.
"""

from __future__ import annotations

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

import ai_team_sync.mcp.server as mcp
from ai_team_sync.database import get_db
from ai_team_sync.server import create_app

ROOT = "/srv/delegated-twin"


def _wire(monkeypatch, db_engine, tmp_path):
    """MCP tools against an in-process app; pointer state in a temp dir."""
    monkeypatch.setenv("ATS_STATE_DIR", str(tmp_path))
    for var in (
        "ATS_AGENT",
        "CLAUDECODE",
        "CLAUDE_CODE",
        "CLAUDE_CODE_SESSION_ID",
        "ATS_SESSION_ID",
        "ATS_DELEGATION",
        "ATS_SESSION",
    ):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setattr(mcp, "_IN_PROCESS_SESSION_ID", None)
    app = create_app()
    factory = async_sessionmaker(db_engine, class_=AsyncSession, expire_on_commit=False)

    async def override_get_db():
        async with factory() as s:
            yield s

    app.dependency_overrides[get_db] = override_get_db
    transport = ASGITransport(app=app)

    class _ASGIClient:
        def __init__(self, *a, **k):
            self._c = AsyncClient(transport=transport, base_url="http://localhost:8400")

        async def __aenter__(self):
            return self._c

        async def __aexit__(self, *exc):
            await self._c.aclose()

    monkeypatch.setattr(mcp.httpx, "AsyncClient", _ASGIClient)
    return transport


async def _bound_child(transport, mode="VERIFY"):
    """Parent session, delegation, and the child row the wrapper registers."""
    async with AsyncClient(transport=transport, base_url="http://localhost:8400") as c:
        parent = await c.post(
            "/api/sessions",
            json={
                "developer": "tester",
                "agent": "claude-code:parent0",
                "scope": [],
                "repo_root": ROOT,
                "description": "parent",
                "auto_lock": False,
            },
        )
        assert parent.status_code == 201, parent.text
        deleg = await c.post(
            "/api/delegations",
            json={
                "parent_session_id": parent.json()["id"],
                "delegated_worker": "codex",
                "mode": mode,
                "objective": "review",
                "acceptance": "verdict",
            },
        )
        assert deleg.status_code == 201, deleg.text
        child = await c.post(
            "/api/sessions",
            json={
                "developer": "tester",
                "agent": "codex:delegate",
                "scope": [],
                "repo_root": ROOT,
                "description": f"delegated {mode}",
                "auto_lock": False,
                "delegation_id": deleg.json()["id"],
            },
        )
        assert child.status_code == 201, child.text
        return deleg.json()["id"], child.json()["id"]


async def _rows(transport):
    async with AsyncClient(transport=transport, base_url="http://localhost:8400") as c:
        r = await c.get("/api/sessions")
        r.raise_for_status()
        return r.json()


def _as_child(monkeypatch, delegation_id, session_id):
    """The environment delegation.child_env gives the spawned worker."""
    monkeypatch.setenv("ATS_DELEGATION", delegation_id)
    monkeypatch.setenv("ATS_SESSION_ID", session_id)
    monkeypatch.setenv("ATS_AGENT", "codex:delegate")


async def _start(scope=(), description="Independent read-only adversarial VERIFY"):
    out = await mcp._call_tool_impl(
        "start_session", {"scope": list(scope), "description": description, "repo_root": ROOT}
    )
    return out[0].text


@pytest.mark.asyncio
async def test_delegated_child_adopts_its_bound_session(db_engine, monkeypatch, tmp_path):
    transport = _wire(monkeypatch, db_engine, tmp_path)
    delegation_id, child = await _bound_child(transport)
    before = {row["id"] for row in await _rows(transport)}
    _as_child(monkeypatch, delegation_id, child)

    text = await _start()

    assert f"Delegated session already open: {child}" in text
    assert {row["id"] for row in await _rows(transport)} == before, "no twin row"


@pytest.mark.asyncio
async def test_delegated_child_scope_request_claims_nothing(db_engine, monkeypatch, tmp_path):
    transport = _wire(monkeypatch, db_engine, tmp_path)
    delegation_id, child = await _bound_child(transport)
    _as_child(monkeypatch, delegation_id, child)

    text = await _start(scope=["src/**"])

    assert "Requested scope was NOT claimed" in text
    rows = {row["id"]: row for row in await _rows(transport)}
    assert rows[child]["scope"] == []
    assert len([r for r in rows.values() if r["agent"] == "codex:delegate"]) == 1


@pytest.mark.asyncio
async def test_unproven_delegation_binding_refuses(db_engine, monkeypatch, tmp_path):
    transport = _wire(monkeypatch, db_engine, tmp_path)
    delegation_id, _ = await _bound_child(transport)
    before = {row["id"] for row in await _rows(transport)}
    _as_child(monkeypatch, delegation_id, "00000000-0000-0000-0000-000000000000")

    text = await _start()

    assert text.startswith("❌ Session start refused")
    assert {row["id"] for row in await _rows(transport)} == before


@pytest.mark.asyncio
async def test_non_delegated_start_session_still_creates(db_engine, monkeypatch, tmp_path):
    transport = _wire(monkeypatch, db_engine, tmp_path)
    monkeypatch.setenv("ATS_AGENT", "claude-code")
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", "abcdef12-0000-0000-0000-000000000000")
    before = len(await _rows(transport))

    text = await _start(scope=[], description="ordinary session")

    assert "✅ Session started!" in text
    assert len(await _rows(transport)) == before + 1
