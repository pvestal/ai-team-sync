"""#2910: a durable decision can be bound to a Tower ticket, and found by it exactly.

Measured 2026-09-20: decision 676a3342 (the #2907 PR #8 review BLOCK) landed with
ticket_id NULL because the log_decision MCP tool had no ticket parameter and the
reviewing session was (rightly) unbound. Task-scoped retrieval then needed
`WHERE ticket_id=2907 OR title LIKE '%2907%'`, and the OR was load-bearing.

Every assertion re-reads the stored row; the tool's success string is not proof.
Both directions are pinned: a bound decision IS in the ticket's result, and an
unrelated decision whose title names the ticket is NOT.
"""

from __future__ import annotations

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

import ai_team_sync.mcp.server as mcp
from ai_team_sync.database import get_db
from ai_team_sync.models import Decision
from ai_team_sync.server import create_app

TICKET, OTHER = 2907, 2899
BASE = "http://localhost:8400"


@pytest.fixture
async def rig(db_engine, monkeypatch):
    app = create_app()
    factory = async_sessionmaker(db_engine, class_=AsyncSession, expire_on_commit=False)

    async def override_get_db():
        async with factory() as s:
            yield s

    app.dependency_overrides[get_db] = override_get_db
    transport = ASGITransport(app=app)

    class _ASGIClient:
        def __init__(self, *a, **k):
            self._c = AsyncClient(transport=transport, base_url=BASE)

        async def __aenter__(self):
            return self._c

        async def __aexit__(self, *exc):
            await self._c.aclose()

    monkeypatch.setattr(mcp.httpx, "AsyncClient", _ASGIClient)
    api = AsyncClient(transport=transport, base_url=BASE)

    async def start(ticket_id=None, role=None):
        body = {"developer": "patrick", "agent": "claude-code", "scope": []}
        if ticket_id:
            body.update(ticket_id=ticket_id, ticket_role=role or "reviewer")
            if body["ticket_role"] == "contributor":
                body["scope"] = [f"docs/{ticket_id}.md"]
        r = await api.post("/api/sessions", json=body)
        assert r.status_code == 201, r.text
        sid = r.json()["id"]
        monkeypatch.setattr(mcp, "_IN_PROCESS_SESSION_ID", sid)
        monkeypatch.setattr(mcp, "_IN_PROCESS_TOKENS", {sid: r.headers["X-ATS-Approval-Token"]})
        return sid

    async def decide(**args):
        out = await mcp.call_tool("log_decision", {"chosen": "c", "reasoning": "r", **args})
        return out[0].text

    async def rows():
        # Only what log_decision wrote: starting a ticket session can record
        # its own decision row (ticket role), which is not under test here.
        async with factory() as s:
            return (await s.execute(select(Decision).where(Decision.chosen == "c")
                                    .order_by(Decision.created_at))).scalars().all()

    async def count():
        async with factory() as s:
            return (await s.execute(select(func.count()).select_from(Decision)
                                    .where(Decision.chosen == "c"))).scalar_one()

    async def by_ticket(ticket_id):
        r = await api.get("/api/decisions", params={"ticket_id": ticket_id})
        assert r.status_code == 200, r.text
        return r.json()

    yield type("Rig", (), {"start": staticmethod(start), "decide": staticmethod(decide),
                           "rows": staticmethod(rows), "count": staticmethod(count),
                           "by_ticket": staticmethod(by_ticket), "api": api})
    await api.aclose()


async def test_an_unbound_reviewer_can_bind_a_decision_to_a_ticket_at_write_time(rig):
    """Acceptance 1 + 2: explicit binding persisted, from a session bound to nothing."""
    await rig.start()  # an independent reviewer: claims no ticket
    text = await rig.decide(title="PR #8 review: BLOCK", ticket_id=TICKET)
    assert "Decision logged" in text, text
    (row,) = await rig.rows()
    assert row.ticket_id == TICKET, "re-read: the binding is on the row"


async def test_a_ticket_scoped_query_returns_the_bound_decision(rig):
    """Acceptance 4(a): found by ticket_id alone; its title never names the ticket."""
    await rig.start()
    await rig.decide(title="review verdict: BLOCK", ticket_id=TICKET)
    (row,) = await rig.rows()
    assert [d["id"] for d in await rig.by_ticket(TICKET)] == [row.id]


async def test_an_unrelated_decision_naming_the_ticket_in_its_title_cannot_leak(rig):
    """Acceptance 4(b): no title or prose matching in the query path."""
    await rig.start()
    await rig.decide(title=f"about #{TICKET}, but not bound to it")           # unbound
    await rig.decide(title=f"#{TICKET} mentioned; bound elsewhere", ticket_id=OTHER)
    await rig.decide(title="the real one", ticket_id=TICKET)
    got = [d for d in await rig.by_ticket(TICKET) if d["chosen"] == "c"]
    assert [d["title"] for d in got] == ["the real one"]
    assert [d["title"] for d in await rig.by_ticket(OTHER) if d["chosen"] == "c"] == [
        f"#{TICKET} mentioned; bound elsewhere"]


async def test_a_bound_session_derives_its_ticket_when_none_is_given(rig):
    await rig.start(ticket_id=TICKET, role="contributor")
    await rig.decide(title="derived")
    (row,) = await rig.rows()
    assert row.ticket_id == TICKET


async def test_an_explicit_binding_agreeing_with_the_session_is_accepted(rig):
    await rig.start(ticket_id=TICKET, role="contributor")
    await rig.decide(title="explicit, same", ticket_id=TICKET)
    (row,) = await rig.rows()
    assert row.ticket_id == TICKET


async def test_a_contradictory_binding_refuses_and_writes_nothing(rig):
    """Acceptance 3: explicit T1 vs session T2 refuses -- no silent choice, no row."""
    await rig.start(ticket_id=OTHER, role="contributor")
    text = await rig.decide(title="contradiction", ticket_id=TICKET)
    assert "Decision NOT logged" in text and "must match its session ticket" in text, text
    assert await rig.count() == 0, "a refused decision left a row"


async def test_unbound_decisions_stay_unbound_and_readable(rig):
    """Acceptance 5: nothing is inferred, nothing dropped from existing surfaces."""
    sid = await rig.start()
    await rig.decide(title=f"historical, mentions #{TICKET}")
    (row,) = await rig.rows()
    assert row.ticket_id is None, "never inferred from the title"
    assert [d["id"] for d in (await rig.api.get("/api/decisions")).json()] == [row.id]
    assert [d["id"] for d in (await rig.api.get(
        "/api/decisions", params={"session_id": sid})).json()] == [row.id]
    assert await rig.by_ticket(TICKET) == []


async def test_get_decision_history_by_ticket_uses_the_binding_only(rig):
    await rig.start()
    await rig.decide(title=f"#{TICKET} in the title only")
    await rig.decide(title="bound", ticket_id=TICKET)
    out = (await mcp.call_tool("get_decision_history", {"ticket_id": TICKET}))[0].text
    assert "bound" in out and "in the title only" not in out, out


@pytest.mark.parametrize("bad", [0, -1, "2907", True])
async def test_a_malformed_ticket_id_is_refused_by_the_tool_and_writes_nothing(rig, bad):
    await rig.start()
    text = await rig.decide(title="bad", ticket_id=bad)
    assert "Decision NOT logged" in text, text
    assert await rig.count() == 0
