"""#3585 slice 1, review round 3: the two races, reproduced on a real ATS server.

1. Simultaneous VERIFY verdict submissions. A recorded verdict is final:
   identical duplicates are idempotent (all get the one recorded result),
   different ones are refused with an explicit conflict, before and after the
   parent reconciles. Many threads hit a real uvicorn server at once.
2. Concurrent ticket-mail delivery and owner completion. Over many rounds, an
   owner completes while ticket-mode sends, a contributor's handoff and the
   owner's own inbox claim run at the same moment. Afterwards no unread ticket
   mail sits on a completed session, and the next legitimate owner claims it all.

Plus the MCP surface of a refused ownership claim (operator issue 3): it names
the ticket's owner, structured, and nothing about unrelated sessions.
"""

from __future__ import annotations

import json
import os
import subprocess
from concurrent.futures import ThreadPoolExecutor

import pytest

import ai_team_sync.mcp.server as mcp
from ai_team_sync import briefs
from ai_team_sync.verdicts import BLOCK
from tests.test_hook_delivery_live import live_ats  # noqa: F401  (fixture)
from tests.test_mcp_caller_identity_2757 import ROOT as MCP_ROOT
from tests.test_mcp_caller_identity_2757 import _wire
from tests.test_task_brief import TASK_ENVELOPE

TICKET = 3585
HEAD = "2ce0ef057b9895e419ef20dc9ac4bd08521722bf"


@pytest.fixture(autouse=True)
def _no_live_echo(monkeypatch):
    monkeypatch.setenv("ATS_EMIT_COMPLETION", "0")
    monkeypatch.setenv("ECHO_BRAIN_URL", "http://127.0.0.1:9")


def auth(token):
    return {"X-ATS-Approval-Token": token}


def _start(client, agent, **extra):
    made = client.post("/api/sessions", json={"developer": "pvestal", "agent": agent,
                                              "scope": [], **extra})
    assert made.status_code == 201, made.text
    return made.json()["id"], made.headers["X-ATS-Approval-Token"], made.json()


def _verdict(head=HEAD, item_id="1", severity="material_safety", category="security"):
    return {"verdict_code": BLOCK,
            "reviewed": {"repo": "/r", "base_sha": "", "head_sha": head},
            "items": [{"item_id": item_id, "status": BLOCK, "severity": severity,
                       "claim_category": category, "citation": "src/x.py:1"}]}


def _verify_with_child(client):
    parent, parent_tok, _ = _start(client, "claude-code:aaaa1111")
    made = client.post("/api/delegations", json={
        "parent_session_id": parent, "delegated_worker": "codex", "mode": "VERIFY",
        "acceptance": "per-item verdict"})
    assert made.status_code == 201, made.text
    delegation = made.json()["id"]
    child, child_tok, _ = _start(client, "codex:delegate", delegation_id=delegation)
    return parent, parent_tok, delegation, child, child_tok


def _submit(client, delegation, child, child_tok, structured):
    return client.post(f"/api/delegations/{delegation}/return", json={
        "result_summary": "see verdict", "actor_session_id": child,
        "structured_verdict": structured}, headers=auth(child_tok))


# --- 1. simultaneous verdict submissions ---------------------------------------------

def test_simultaneous_identical_submissions_are_idempotent(live_ats):
    url, client, _ = live_ats
    _, _, delegation, child, child_tok = _verify_with_child(client)
    same = _verdict()
    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(lambda _: _submit(client, delegation, child, child_tok, same),
                                range(8)))
    assert [r.status_code for r in results] == [200] * 8, [r.text for r in results]
    recorded = {json.dumps(r.json()["structured_verdict"], sort_keys=True) for r in results}
    assert len(recorded) == 1
    row = client.get(f"/api/delegations/{delegation}").json()
    assert row["state"] == "returned" and row["effective_verdict"] == BLOCK
    assert row["structured_verdict"]["reviewed"]["head_sha"] == HEAD


def test_simultaneous_different_submissions_have_one_winner_and_explicit_conflicts(live_ats):
    url, client, _ = live_ats
    _, _, delegation, child, child_tok = _verify_with_child(client)
    variants = [_verdict(item_id=str(n)) for n in range(8)]
    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(
            lambda v: _submit(client, delegation, child, child_tok, v), variants))
    codes = sorted(r.status_code for r in results)
    assert codes == [200] + [409] * 7, [r.text for r in results]
    assert all(r.json()["detail"]["error"] == "verdict_conflict"
               for r in results if r.status_code == 409)
    winner = next(r.json() for r in results if r.status_code == 200)
    row = client.get(f"/api/delegations/{delegation}").json()
    assert row["structured_verdict"] == winner["structured_verdict"]


def test_a_recorded_verdict_never_changes_even_after_reconciliation(live_ats):
    url, client, _ = live_ats
    parent, parent_tok, delegation, child, child_tok = _verify_with_child(client)
    first = _verdict()
    assert _submit(client, delegation, child, child_tok, first).status_code == 200
    assert _submit(client, delegation, child, child_tok, first).status_code == 200
    other = _submit(client, delegation, child, child_tok, _verdict(item_id="2"))
    assert other.status_code == 409 and other.json()["detail"]["error"] == "verdict_conflict"
    assert other.json()["detail"]["recorded_effective_verdict"] == BLOCK
    closed = client.post(f"/api/delegations/{delegation}/close", json={
        "state": "closed", "verdict": "accepted", "actor_session_id": parent})
    assert closed.status_code == 200, closed.text
    assert _submit(client, delegation, child, child_tok, first).status_code == 200
    after = _submit(client, delegation, child, child_tok, _verdict(item_id="3"))
    assert after.status_code == 409
    row = client.get(f"/api/delegations/{delegation}").json()
    assert row["state"] == "closed" and row["structured_verdict"]["items"][0]["item_id"] == "1"
    # A bystander cannot even probe the recorded verdict through this door.
    bystander, bystander_tok, _ = _start(client, "codex:eeee5555")
    probe = _submit(client, delegation, bystander, bystander_tok, first)
    assert probe.status_code == 403


# --- 2. concurrent delivery and owner completion -------------------------------------

@pytest.fixture
def repo(tmp_path):
    root = tmp_path / "repo"
    root.mkdir()
    env = dict(os.environ, GIT_CONFIG_GLOBAL=os.devnull, GIT_CONFIG_NOSYSTEM="1",
               GIT_AUTHOR_NAME="t", GIT_AUTHOR_EMAIL="t@t", GIT_COMMITTER_NAME="t",
               GIT_COMMITTER_EMAIL="t@t")
    subprocess.run(["git", "init", "-q", "-b", "main", str(root)], check=True, env=env)
    for n in range(40):
        (root / f"f{n}.txt").write_text("x\n")
    subprocess.run(["git", "-C", str(root), "add", "."], check=True, env=env)
    subprocess.run(["git", "-C", str(root), "commit", "-qm", "init"], check=True, env=env)
    return os.path.realpath(root)


def test_owner_completion_racing_delivery_never_strands_ticket_mail(live_ats, repo):
    url, client, _ = live_ats
    sender, sender_tok, _ = _start(client, "codex:5e5e5e5e", ticket_id=TICKET,
                                   ticket_role="reviewer")
    owner, owner_tok, _ = _start(client, "claude-code:00000000", ticket_id=TICKET,
                                 ticket_role="owner")
    sent_ids: list[str] = []
    for round_no in range(12):
        contrib, contrib_tok, _ = _start(
            client, f"codex:c{round_no:07d}", ticket_id=TICKET, ticket_role="contributor",
            scope=[f"f{round_no}.txt"], repo_root=repo)

        def send(n, round_no=round_no):
            return client.post("/api/messages", json={
                "sender_session_id": sender, "ticket_id": TICKET,
                "body": f"round {round_no} message {n}"}, headers=auth(sender_tok))

        def complete_owner(owner=owner, owner_tok=owner_tok):
            return client.patch(f"/api/sessions/{owner}", json={"status": "completed"},
                                headers=auth(owner_tok))

        def handoff(contrib=contrib, contrib_tok=contrib_tok, round_no=round_no):
            return client.patch(f"/api/sessions/{contrib}", json={
                "status": "completed", "handoff": {"verdict": f"round {round_no} done"}},
                headers=auth(contrib_tok))

        def owner_reads(owner=owner, owner_tok=owner_tok):
            return client.get(f"/api/sessions/{owner}/messages", headers=auth(owner_tok))

        with ThreadPoolExecutor(max_workers=8) as pool:
            jobs = [pool.submit(send, n) for n in range(4)]
            jobs += [pool.submit(owner_reads), pool.submit(complete_owner),
                     pool.submit(handoff), pool.submit(owner_reads)]
            results = [j.result() for j in jobs]
        sends = results[:4]
        assert all(r.status_code == 201 for r in sends), [r.text for r in sends]
        assert results[5].status_code == 200 and results[6].status_code == 200
        sent_ids += [r.json()["id"] for r in sends]

        # Nothing unread may sit on a completed session.
        for mid in sent_ids:
            status = client.get(f"/api/messages/{mid}", params={"sender_session_id": sender},
                                headers=auth(sender_tok)).json()
            if status["acknowledged_at"] is None and status["recipient_session_id"]:
                holder = client.get(f"/api/sessions/{status['recipient_session_id']}").json()
                assert holder["status"] != "completed", (round_no, mid, status)
        handoffs = client.get(f"/api/tickets/{TICKET}/handoffs").json()
        for h in handoffs:
            if h["acknowledged_at"] is None and h["recipient_session_id"]:
                holder = client.get(f"/api/sessions/{h['recipient_session_id']}").json()
                assert holder["status"] != "completed", (round_no, h)

        # The next legitimate owner claims every unread message and handoff.
        owner, owner_tok, row = _start(client, f"claude-code:{round_no + 1:08d}",
                                       ticket_id=TICKET, ticket_role="owner")
        inbox = {m["id"]: m for m in client.get(f"/api/sessions/{owner}/messages",
                                                params={"limit": 100},
                                                headers=auth(owner_tok)).json()}
        assert set(sent_ids) <= set(inbox), (round_no, set(sent_ids) - set(inbox))
        assert any(m["kind"] == "handoff" and f"round {round_no} done" in m["body"]
                   for m in inbox.values()), round_no


@pytest.mark.asyncio
async def test_an_inbox_claim_paused_across_the_owners_completion_strands_nothing(
        tmp_path, monkeypatch):
    """Deterministic reproduction of the round-2 window (Codex N2).

    The stress test above may never hit it, so this forces the interleaving.
    The real ATS app runs in-process on a FILE database (each request has its
    own connection, as in production); the owner's inbox claim is paused just
    before it claims, while the same owner completes. Before the fix the claim
    then assigned pooled mail to the completed owner. Now the completion waits
    for the claim (shared acquisition guard) and releases what it claimed.
    """
    import asyncio

    from httpx import ASGITransport, AsyncClient
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
    from sqlalchemy.pool import NullPool

    from ai_team_sync import ticket_roles
    from ai_team_sync.database import get_db
    from ai_team_sync.models import AgentMessage, Base
    from ai_team_sync.server import create_app

    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'ats.db'}",
                                 poolclass=NullPool,
                                 connect_args={"timeout": 30})
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    app = create_app()

    async def file_db():
        async with factory() as session:
            yield session

    app.dependency_overrides[get_db] = file_db
    reached, completed = asyncio.Event(), asyncio.Event()
    real_claim = ticket_roles.claim_pool

    async def paused_claim(db, session):
        reached.set()
        try:
            await asyncio.wait_for(completed.wait(), timeout=1.5)
        except asyncio.TimeoutError:
            pass  # the completion is (correctly) waiting for this claim
        return await real_claim(db, session)

    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
            async def new(agent, **extra):
                r = await c.post("/api/sessions", json={
                    "developer": "pvestal", "agent": agent, "scope": [],
                    "ticket_id": TICKET, **extra})
                assert r.status_code == 201, r.text
                return r.json()["id"], r.headers["X-ATS-Approval-Token"]

            owner, owner_tok = await new("claude-code:00000001", ticket_role="owner")
            sender, _ = await new("codex:5e5e5e5e", ticket_role="reviewer")
            # Pooled mail with a live owner: the outcome of a send that raced
            # the owner's start, which the inbox claim exists to collect.
            async with factory() as db:
                pooled = AgentMessage(sender_session_id=sender, recipient_session_id=None,
                                      original_recipient_session_id=None,
                                      addressing_mode="ticket", ticket_id=TICKET,
                                      kind="handoff", sender_agent="codex:5e5e5e5e",
                                      sender_developer="pvestal", recipient_agent="",
                                      body="raced the owner's start")
                db.add(pooled)
                await db.commit()
                pooled_id = pooled.id

            monkeypatch.setattr(ticket_roles, "claim_pool", paused_claim)

            async def owner_reads():
                return await c.get(f"/api/sessions/{owner}/messages", headers=auth(owner_tok))

            async def owner_completes():
                await reached.wait()
                done = await c.patch(f"/api/sessions/{owner}", json={"status": "completed"},
                                     headers=auth(owner_tok))
                completed.set()
                return done

            read, done = await asyncio.gather(owner_reads(), owner_completes())
            assert read.status_code == 200 and done.status_code == 200, (read.text, done.text)
            monkeypatch.setattr(ticket_roles, "claim_pool", real_claim)

            async with factory() as db:
                row = await db.get(AgentMessage, pooled_id)
                assert row.recipient_session_id != owner, \
                    "unread ticket mail stranded on the completed owner"
            successor, successor_tok = await new("claude-code:00000002", ticket_role="owner")
            inbox = await c.get(f"/api/sessions/{successor}/messages",
                                headers=auth(successor_tok))
            assert pooled_id in {m["id"] for m in inbox.json()}
    finally:
        await engine.dispose()


# --- 3. the MCP surface of a refused ownership claim ------------------------------------

@pytest.mark.asyncio
async def test_mcp_ownership_refusal_names_only_the_tickets_owner(db_engine, monkeypatch, tmp_path):
    from httpx import AsyncClient

    transport = _wire(monkeypatch, db_engine, tmp_path)
    monkeypatch.setenv("ATS_AGENT", "claude-code")
    monkeypatch.setattr(briefs, "fetch_tower_task_data",
                        lambda task_id, **kw: (TASK_ENVELOPE, None))
    monkeypatch.setattr(briefs, "recall_memories", lambda *a, **kw: [])
    ticket = TASK_ENVELOPE["task_context"]["task"]["id"]
    async with AsyncClient(transport=transport, base_url="http://localhost:8400") as c:
        held = await c.post("/api/sessions", json={
            "developer": "pvestal", "agent": "claude-code:0wner000", "scope": [],
            "ticket_id": ticket, "ticket_role": "owner"})
        unrelated = await c.post("/api/sessions", json={
            "developer": "pvestal", "agent": "codex:unrelated", "scope": [],
            "ticket_id": ticket + 1, "ticket_role": "owner"})
    owner_id, unrelated_id = held.json()["id"], unrelated.json()["id"]

    out = (await mcp.call_tool("start_session", {
        "scope": [], "description": "second owner", "repo_root": MCP_ROOT,
        "ticket_id": ticket, "ticket_role": "owner"}))[0].text

    assert "already has a live owner" in out
    assert owner_id in out and "claude-code:0wner000" in out
    assert "conflicts detected" not in out
    structured = json.loads(out.split("structured: ", 1)[1])
    assert structured["error"] == "ticket_owned"
    assert structured["owner_session_id"] == owner_id
    assert unrelated_id not in out and "codex:unrelated" not in out
