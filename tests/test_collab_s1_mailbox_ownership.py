"""#3585 slice 1: ticket roles and the owner-routed ticket mailbox.

Most tests run several agents against a REAL ATS server (uvicorn on a temporary
SQLite file, tests.test_hook_delivery_live.live_ats): each agent is its own
session with its own capability token, exactly as Claude and Codex sessions
are. The few that need the reaper or a forged peer account use the in-process
app, whose single shared connection is fine for sequential requests.

What is pinned here:
  - one live owner per ticket (per OS account), atomically, across threads and
    processes; a second owner is refused naming the holder;
  - only the owner receives the ticket's GENERAL (ticket-mode) mailbox, at send
    time if it is live, by claim when it starts if it was not;
  - contributors and reviewers never receive or drain it;
  - mail addressed to an exact session, addressed decisions and ticket events
    keep their exact recipient whatever its role (operator refinement 1);
  - callers that send no role keep working (first live session owns).
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import textwrap
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from ai_team_sync import peer_identity, ticket_roles
from ai_team_sync.background_tasks import auto_complete_stale_sessions
from ai_team_sync.config import settings
from ai_team_sync.models import Session
from tests.test_hook_delivery_live import live_ats  # noqa: F401  (fixture)

TICKET = 3585
ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture(autouse=True)
def _no_live_echo(monkeypatch):
    # A completed session posts an event to Echo Brain unless told not to; the
    # temporary server inherits this environment, so tests never reach :8309.
    monkeypatch.setenv("ATS_EMIT_COMPLETION", "0")
    monkeypatch.setenv("ECHO_BRAIN_URL", "http://127.0.0.1:9")


@pytest.fixture
def repo(tmp_path):
    root = tmp_path / "repo"
    root.mkdir()
    env = dict(os.environ, GIT_CONFIG_GLOBAL=os.devnull, GIT_CONFIG_NOSYSTEM="1",
               GIT_AUTHOR_NAME="t", GIT_AUTHOR_EMAIL="t@t", GIT_COMMITTER_NAME="t",
               GIT_COMMITTER_EMAIL="t@t")
    subprocess.run(["git", "init", "-q", "-b", "main", str(root)], check=True, env=env)
    for name in ("a.txt", "b.txt", "c.txt"):
        (root / name).write_text(f"{name}\n")
    subprocess.run(["git", "-C", str(root), "add", "."], check=True, env=env)
    subprocess.run(["git", "-C", str(root), "commit", "-qm", "init"], check=True, env=env)
    return os.path.realpath(root)


def auth(token):
    return {"X-ATS-Approval-Token": token}


def start(client, agent, *, role=None, scope=(), repo_root="", ticket=TICKET, expect=201):
    made = client.post("/api/sessions", json={
        "developer": "pvestal", "agent": agent, "scope": list(scope),
        "repo_root": repo_root, "ticket_id": ticket, "ticket_role": role})
    assert made.status_code == expect, made.text
    if expect != 201:
        return made.json()
    body = made.json()
    return body["id"], made.headers["X-ATS-Approval-Token"], body


def send_ticket(client, sid, token, body="ticket note", ticket=TICKET):
    sent = client.post("/api/messages", json={
        "sender_session_id": sid, "ticket_id": ticket, "body": body}, headers=auth(token))
    assert sent.status_code == 201, sent.text
    return sent.json()


def send_direct(client, sid, token, to, body):
    sent = client.post("/api/messages", json={
        "sender_session_id": sid, "recipient_session_id": to, "body": body},
        headers=auth(token))
    assert sent.status_code == 201, sent.text
    return sent.json()


def inbox(client, sid, token):
    got = client.get(f"/api/sessions/{sid}/messages", headers=auth(token))
    assert got.status_code == 200, got.text
    return got.json()


def ids(rows):
    return {row["id"] for row in rows}


def complete(client, sid, token, handoff=None):
    body = {"status": "completed", "summary": "done"}
    if handoff:
        body["handoff"] = handoff
    done = client.patch(f"/api/sessions/{sid}", json=body, headers=auth(token))
    assert done.status_code == 200, done.text


def status_of(client, sender, sender_token, mid):
    got = client.get(f"/api/messages/{mid}", params={"sender_session_id": sender},
                     headers=auth(sender_token))
    assert got.status_code == 200, got.text
    return got.json()


def hook_output(server_env, url, state, cid, sid, token):
    """The real UserPromptSubmit inbox hook, as a subprocess, for one agent."""
    state.mkdir(mode=0o700, exist_ok=True)
    (state / f".ats_session_{cid[:8]}").write_text(sid)
    (state / f".ats_approval_{cid[:8]}").write_text(json.dumps({"session_id": sid,
                                                                "token": token}))
    env = dict(server_env, ATS_STATE_DIR=str(state), ATS_SERVER_URL=url,
               CLAUDE_CODE_SESSION_ID=cid, ATS_AGENT="claude-code")
    env.pop("ATS_SESSION_ID", None)
    env.pop("ATS_SESSION", None)
    run = subprocess.run([sys.executable, "-m", "ai_team_sync.hooks.override_inbox"],
                         input=json.dumps({"session_id": cid}), text=True,
                         capture_output=True, env=env, timeout=20)
    assert run.returncode == 0, run.stderr
    return run.stdout


# --- (a) owner, contributor, reviewer --------------------------------------------

def test_only_the_owner_receives_general_ticket_mail(live_ats, repo, tmp_path):
    url, client, server_env = live_ats
    owner, owner_tok, owner_row = start(client, "claude-code:aaaa1111", role="owner",
                                        scope=["a.txt"], repo_root=repo)
    contrib, contrib_tok, contrib_row = start(client, "claude-code:bbbb2222",
                                              role="contributor", scope=["b.txt"],
                                              repo_root=repo)
    reviewer, reviewer_tok, reviewer_row = start(client, "codex:cccc3333", role="reviewer")
    sender, sender_tok, _ = start(client, "codex:dddd4444", role="reviewer")
    assert (owner_row["ticket_role"], contrib_row["ticket_role"],
            reviewer_row["ticket_role"]) == ("owner", "contributor", "reviewer")
    assert contrib_row["ticket_owner_session_id"] == owner
    assert reviewer_row["ticket_owner_session_id"] == owner

    note = send_ticket(client, sender, sender_tok, "general ticket instruction")
    assert note["recipient_session_id"] == owner
    assert note["delivery_history"][-1]["reason"] == "owner_route"

    # Reading never drains the owner's mailbox for a contributor or reviewer.
    assert note["id"] not in ids(inbox(client, contrib, contrib_tok))
    assert note["id"] not in ids(inbox(client, reviewer, reviewer_tok))
    assert note["id"] in ids(inbox(client, owner, owner_tok))

    # Through the real UserPromptSubmit hook: the owner's turn shows it, the
    # contributor's does not.
    owner_turn = hook_output(server_env, url, tmp_path / "s-owner",
                             "aaaa1111-0000-0000-0000-000000000001", owner, owner_tok)
    contrib_turn = hook_output(server_env, url, tmp_path / "s-contrib",
                               "bbbb2222-0000-0000-0000-000000000002", contrib, contrib_tok)
    assert note["id"] in owner_turn and "general ticket instruction" in owner_turn
    assert note["id"] not in contrib_turn

    # Handoffs written by a finishing contributor or reviewer go to the owner.
    complete(client, contrib, contrib_tok, {"verdict": "b.txt done", "next_steps": ["merge"]})
    complete(client, reviewer, reviewer_tok, {"verdict": "reviewed", "blockers": []})
    handoffs = [m for m in inbox(client, owner, owner_tok) if m["kind"] == "handoff"]
    assert {m["sender_session_id"] for m in handoffs} == {contrib, reviewer}
    assert all(m["delivery_history"][-1]["reason"] == "owner_route" for m in handoffs)


# --- (g) addressed mail keeps its exact recipient ------------------------------------

def test_addressed_mail_decisions_and_events_keep_their_exact_recipient(live_ats, repo):
    url, client, _ = live_ats
    owner, owner_tok, _ = start(client, "claude-code:aaaa1111", role="owner",
                                scope=["a.txt"], repo_root=repo)
    contrib, contrib_tok, _ = start(client, "claude-code:bbbb2222", role="contributor",
                                    scope=["b.txt"], repo_root=repo)
    reviewer, reviewer_tok, _ = start(client, "codex:cccc3333", role="reviewer")
    sender, sender_tok, _ = start(client, "codex:dddd4444", role="reviewer")

    to_contrib = send_direct(client, sender, sender_tok, contrib, "for the contributor")
    to_reviewer = send_direct(client, sender, sender_tok, reviewer, "for the reviewer")
    decision = client.post("/api/decisions", json={
        "session_id": owner, "recipient_session_id": reviewer, "ticket_id": TICKET,
        "title": "review scope", "chosen": "review a.txt only", "reasoning": "slice 1"},
        headers=auth(owner_tok))
    assert decision.status_code == 201, decision.text

    contrib_rows = inbox(client, contrib, contrib_tok)
    reviewer_rows = inbox(client, reviewer, reviewer_tok)
    owner_rows = inbox(client, owner, owner_tok)
    assert to_contrib["id"] in ids(contrib_rows)
    assert to_reviewer["id"] in ids(reviewer_rows)
    assert any(m["kind"] == "decision" for m in reviewer_rows)
    for row in (to_contrib, to_reviewer):
        assert row["id"] not in ids(owner_rows)
        assert row["addressing_mode"] == "session"
        assert row["delivery_history"][-1]["reason"] == "direct_send"
    assert not any(m["kind"] == "decision" for m in owner_rows)

    # Ticket events stay one-per-peer: the contributor heard the reviewer start.
    assert any(m["kind"] == "event" and reviewer in m["body"] for m in contrib_rows)


# --- (b) late-arriving handoffs ----------------------------------------------------

def test_a_handoff_sent_after_the_owner_started_reaches_it_at_send_time(live_ats, repo):
    url, client, _ = live_ats
    owner, owner_tok, _ = start(client, "claude-code:aaaa1111", role="owner",
                                scope=["a.txt"], repo_root=repo)
    late, late_tok, _ = start(client, "codex:eeee5555", role="reviewer")
    complete(client, late, late_tok, {"verdict": "late finding", "blockers": ["x"]})
    rows = [m for m in inbox(client, owner, owner_tok) if m["kind"] == "handoff"]
    assert len(rows) == 1 and rows[0]["sender_session_id"] == late
    assert [e["reason"] for e in rows[0]["delivery_history"]] == ["owner_route"]
    chain = client.get(f"/api/tickets/{TICKET}/handoffs").json()
    assert chain[-1]["recipient_session_id"] == owner


def test_ownerless_ticket_mail_waits_until_an_owner_starts(live_ats, repo):
    url, client, _ = live_ats
    sender, sender_tok, _ = start(client, "codex:ffff6666", role="reviewer")
    queued = send_ticket(client, sender, sender_tok, "waiting for an owner")
    assert queued["recipient_session_id"] is None

    contrib, contrib_tok, contrib_row = start(client, "claude-code:bbbb2222",
                                              role="contributor", scope=["b.txt"],
                                              repo_root=repo)
    assert contrib_row["ticket_owner_session_id"] is None
    assert queued["id"] not in ids(inbox(client, contrib, contrib_tok))
    assert status_of(client, sender, sender_tok, queued["id"])["recipient_session_id"] is None

    owner, owner_tok, _ = start(client, "claude-code:aaaa1111", role="owner",
                                scope=["a.txt"], repo_root=repo)
    rows = {m["id"]: m for m in inbox(client, owner, owner_tok)}
    assert queued["id"] in rows
    assert rows[queued["id"]]["delivery_history"][-1]["reason"] == "ticket_claim"
    assert queued["id"] not in ids(inbox(client, contrib, contrib_tok))


def test_explicit_completion_returns_unread_mail_to_the_next_owner(live_ats, repo):
    url, client, _ = live_ats
    owner, owner_tok, _ = start(client, "claude-code:aaaa1111", role="owner",
                                scope=["a.txt"], repo_root=repo)
    sender, sender_tok, _ = start(client, "codex:ffff6666", role="reviewer")
    note = send_ticket(client, sender, sender_tok, "unread when the owner left")
    assert note["recipient_session_id"] == owner
    complete(client, owner, owner_tok)
    assert status_of(client, sender, sender_tok, note["id"])["recipient_session_id"] is None
    successor, successor_tok, row = start(client, "claude-code:9999aaaa")
    assert row["ticket_role"] == "owner"
    assert note["id"] in ids(inbox(client, successor, successor_tok))


# --- one owner, atomically ---------------------------------------------------------

def test_a_second_owner_is_refused_naming_the_holder(live_ats, repo):
    url, client, _ = live_ats
    owner, owner_tok, _ = start(client, "claude-code:aaaa1111", role="owner",
                                scope=["a.txt"], repo_root=repo)
    refused = start(client, "claude-code:bbbb2222", role="owner", expect=409)
    assert refused["detail"]["error"] == "ticket_owned"
    assert refused["detail"]["owner_session_id"] == owner
    complete(client, owner, owner_tok)
    _, _, row = start(client, "claude-code:bbbb2222", role="owner")
    assert row["ticket_role"] == "owner"


def test_concurrent_owner_requests_from_threads_have_exactly_one_winner(live_ats):
    url, client, _ = live_ats

    def request(n):
        return client.post("/api/sessions", json={
            "developer": "pvestal", "agent": f"claude-code:{n:08d}", "scope": [],
            "ticket_id": TICKET, "ticket_role": "owner"})

    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(request, range(8)))
    codes = sorted(r.status_code for r in results)
    assert codes == [201] + [409] * 7, [r.text for r in results]
    assert all(r.json()["detail"]["error"] == "ticket_owned"
               for r in results if r.status_code == 409)


def test_concurrent_role_less_starts_elect_exactly_one_owner(live_ats):
    url, client, _ = live_ats

    def request(n):
        return client.post("/api/sessions", json={
            "developer": "pvestal", "agent": f"claude-code:{n:08d}", "scope": [],
            "ticket_id": TICKET})

    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(request, range(8)))
    assert all(r.status_code == 201 for r in results), [r.text for r in results]
    roles = sorted(r.json()["ticket_role"] for r in results)
    # Unscoped joiners claim nothing, so they join as reviewers.
    assert roles == ["owner"] + ["reviewer"] * 7
    owner = next(r.json()["id"] for r in results if r.json()["ticket_role"] == "owner")
    assert all(r.json()["ticket_owner_session_id"] == owner for r in results)


def test_concurrent_owner_requests_from_separate_processes_have_one_winner(live_ats, tmp_path):
    url, _client, server_env = live_ats
    go = tmp_path / "go"
    script = textwrap.dedent(f"""
        import json, os, sys, time, httpx
        while not os.path.exists({str(go)!r}):
            time.sleep(0.005)
        r = httpx.post({url!r} + "/api/sessions", json={{
            "developer": "pvestal", "agent": "claude-code:" + sys.argv[1], "scope": [],
            "ticket_id": {TICKET}, "ticket_role": "owner"}}, timeout=20)
        print(json.dumps({{"code": r.status_code, "body": r.json()}}))
    """)
    procs = [subprocess.Popen([sys.executable, "-c", script, f"{n:08d}"],
                              stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                              env=server_env) for n in range(6)]
    go.write_text("go")
    outs = [json.loads(p.communicate(timeout=60)[0]) for p in procs]
    codes = sorted(o["code"] for o in outs)
    assert codes == [201] + [409] * 5, outs


# --- (f) callers without a role, and role validation ----------------------------------

def test_callers_that_send_no_role_keep_working(live_ats, repo):
    url, client, _ = live_ats
    first, first_tok, first_row = start(client, "claude-code:aaaa1111")
    second, second_tok, second_row = start(client, "claude-code:bbbb2222")
    third, third_tok, third_row = start(client, "claude-code:cccc4444", scope=["b.txt"],
                                        repo_root=repo)
    assert first_row["ticket_role"] == "owner"
    assert first_row["ticket_owner_session_id"] == first
    # Every contributor is scoped: unscoped joiners are reviewers.
    assert second_row["ticket_role"] == "reviewer"
    assert third_row["ticket_role"] == "contributor"
    assert second_row["ticket_owner_session_id"] == third_row["ticket_owner_session_id"] == first
    unbound = client.post("/api/sessions", json={
        "developer": "pvestal", "agent": "claude-code:cccc3333", "scope": []})
    assert unbound.status_code == 201 and unbound.json()["ticket_role"] is None


def test_roles_are_validated(live_ats, repo):
    url, client, _ = live_ats
    assert start(client, "codex:aaaa1111", role="reviewer", scope=["a.txt"],
                 repo_root=repo, expect=422)["detail"]["error"] == "reviewer_claims_nothing"
    assert start(client, "claude-code:bbbb2222", role="contributor",
                 expect=422)["detail"]["error"] == "contributor_needs_scope"
    assert start(client, "claude-code:cccc3333", role="owner", ticket=None,
                 expect=422)["detail"]["error"] == "role_requires_ticket"
    assert start(client, "claude-code:dddd4444", role="captain", expect=422)


def test_a_paused_owner_still_owns(live_ats):
    url, client, _ = live_ats
    owner, owner_tok, _ = start(client, "claude-code:aaaa1111", role="owner")
    paused = client.patch(f"/api/sessions/{owner}", json={"status": "paused"},
                          headers=auth(owner_tok))
    assert paused.status_code == 200, paused.text
    refused = start(client, "claude-code:bbbb2222", role="owner", expect=409)
    assert refused["detail"]["owner_session_id"] == owner


# --- revival never makes a second owner (in-process: needs the reaper) -------------------

async def _new(client, agent, **extra):
    made = await client.post("/api/sessions", json={
        "developer": "pvestal", "agent": agent, "scope": [], "ticket_id": TICKET, **extra})
    assert made.status_code == 201, made.text
    return made.json(), made.headers["X-ATS-Approval-Token"]


async def _reap(db_session, sid):
    row = await db_session.get(Session, sid)
    now = datetime.now(timezone.utc)
    row.started_at = now - timedelta(hours=1)
    row.last_heartbeat = now - timedelta(minutes=settings.session_heartbeat_timeout_minutes + 2)
    await db_session.commit()
    assert await auto_complete_stale_sessions(db_session) == 1


@pytest.mark.asyncio
async def test_a_revived_owner_loses_ownership_and_its_general_mail_when_the_ticket_was_taken(
        client, db_session):
    old, old_tok = await _new(client, "claude-code:aaaa1111", ticket_role="owner")
    sender, sender_tok = await _new(client, "codex:cccc3333", ticket_role="reviewer")
    note = (await client.post("/api/messages", json={
        "sender_session_id": sender["id"], "ticket_id": TICKET, "body": "for the owner"},
        headers=auth(sender_tok))).json()
    assert note["recipient_session_id"] == old["id"]
    await _reap(db_session, old["id"])
    new, new_tok = await _new(client, "claude-code:bbbb2222", ticket_role="owner")
    revived = await client.post(f"/api/sessions/{old['id']}/heartbeat")
    assert revived.status_code == 200, revived.text
    assert revived.json()["status"] == "active"
    assert revived.json()["ticket_role"] == "reviewer"  # it claims no files
    owners = [s for s in (await client.get("/api/sessions")).json()
              if s.get("ticket_id") == TICKET and s.get("ticket_role") == "owner"
              and s["status"] in ("active", "paused")]
    assert [s["id"] for s in owners] == [new["id"]]
    # Operator refinement 1: a non-owner never holds general ticket mail.
    old_inbox = (await client.get(f"/api/sessions/{old['id']}/messages",
                                  headers=auth(old_tok))).json()
    new_inbox = (await client.get(f"/api/sessions/{new['id']}/messages",
                                  headers=auth(new_tok))).json()
    assert note["id"] not in {m["id"] for m in old_inbox}
    moved = next(m for m in new_inbox if m["id"] == note["id"])
    assert [(e["action"], e["reason"]) for e in moved["delivery_history"]][-2:] == [
        ("released", "not_owner"), ("assigned", "owner_route")]


@pytest.mark.asyncio
async def test_a_revived_owner_keeps_ownership_when_nobody_took_it(client, db_session):
    old, _ = await _new(client, "claude-code:aaaa1111", ticket_role="owner")
    await _reap(db_session, old["id"])
    revived = await client.post(f"/api/sessions/{old['id']}/heartbeat")
    assert revived.status_code == 200 and revived.json()["ticket_role"] == "owner"


@pytest.mark.asyncio
async def test_a_reaped_owner_reopened_by_patch_returns_as_a_non_owner(client, db_session):
    old, old_tok = await _new(client, "claude-code:aaaa1111", ticket_role="owner")
    await _reap(db_session, old["id"])
    await _new(client, "claude-code:bbbb2222", ticket_role="owner")
    reopened = await client.patch(f"/api/sessions/{old['id']}", json={"status": "active"},
                                  headers=auth(old_tok))
    assert reopened.status_code == 200, reopened.text
    assert reopened.json()["ticket_role"] == "reviewer"


@pytest.mark.asyncio
async def test_a_reopen_that_races_a_new_owner_is_409_not_500(client, db_session, monkeypatch):
    # Force the race: the demotion check reads "no other owner", then the
    # unique index meets the owner that exists. Must be a clean 409.
    old, old_tok = await _new(client, "claude-code:aaaa1111", ticket_role="owner")
    await _reap(db_session, old["id"])
    await _new(client, "claude-code:bbbb2222", ticket_role="owner")

    async def stale_check(db, session):
        return False

    real_yield = ticket_roles.yield_ownership_if_taken
    monkeypatch.setattr(ticket_roles, "yield_ownership_if_taken", stale_check)
    reopened = await client.patch(f"/api/sessions/{old['id']}", json={"status": "active"},
                                  headers=auth(old_tok))
    assert reopened.status_code == 409, reopened.text
    assert reopened.json()["detail"]["error"] == "ticket_owned"
    # Restore only this patch: undo() would also drop conftest's identity patch.
    monkeypatch.setattr(ticket_roles, "yield_ownership_if_taken", real_yield)
    retried = await client.patch(f"/api/sessions/{old['id']}", json={"status": "active"},
                                 headers=auth(old_tok))
    assert retried.status_code == 200 and retried.json()["ticket_role"] == "reviewer"


@pytest.mark.asyncio
async def test_an_owner_start_that_races_past_the_guard_is_409_not_500(client, monkeypatch):
    await _new(client, "claude-code:aaaa1111", ticket_role="owner")

    async def nobody(*args, **kwargs):
        return None

    monkeypatch.setattr(ticket_roles, "live_owner", nobody)
    raced = await client.post("/api/sessions", json={
        "developer": "pvestal", "agent": "claude-code:bbbb2222", "scope": [],
        "ticket_id": TICKET, "ticket_role": "owner"})
    assert raced.status_code == 409, raced.text
    assert raced.json()["detail"]["error"] == "ticket_owned"


# --- a send that races the owner's completion never strands mail ---------------------

@pytest.mark.asyncio
async def test_mail_routed_to_an_owner_that_completed_meanwhile_returns_to_the_pool(
        client, monkeypatch):
    owner, owner_tok = await _new(client, "claude-code:aaaa1111", ticket_role="owner")
    sender, sender_tok = await _new(client, "codex:cccc3333", ticket_role="reviewer")
    done = await client.patch(f"/api/sessions/{owner['id']}", json={"status": "completed"},
                              headers=auth(owner_tok))
    assert done.status_code == 200, done.text
    # The send read the owner before its completion landed (forced stale read).
    real_live_owner = ticket_roles.live_owner
    calls = {"n": 0}

    async def stale_then_real(db, ticket_id, creator_uid, *, exclude_id=None):
        calls["n"] += 1
        if calls["n"] == 1:
            return await db.get(Session, owner["id"])
        return await real_live_owner(db, ticket_id, creator_uid, exclude_id=exclude_id)

    monkeypatch.setattr(ticket_roles, "live_owner", stale_then_real)
    sent = await client.post("/api/messages", json={
        "sender_session_id": sender["id"], "ticket_id": TICKET, "body": "raced"},
        headers=auth(sender_tok))
    assert sent.status_code == 201, sent.text
    assert sent.json()["recipient_session_id"] is None
    assert [(e["action"], e["reason"]) for e in sent.json()["delivery_history"]] == [
        ("assigned", "owner_route"), ("released", "recipient_completed")]
    monkeypatch.setattr(ticket_roles, "live_owner", real_live_owner)
    successor, successor_tok = await _new(client, "claude-code:bbbb2222")
    inbox_rows = (await client.get(f"/api/sessions/{successor['id']}/messages",
                                   headers=auth(successor_tok))).json()
    assert sent.json()["id"] in {m["id"] for m in inbox_rows}


# --- ownership is per OS account (ATS's trust boundary, #2741) ---------------------------

@pytest.mark.asyncio
async def test_another_account_cannot_take_or_block_this_accounts_ticket_mail(client, monkeypatch):
    peer = {"uid": 1001}
    monkeypatch.setattr(peer_identity, "peer_uid_for_request", lambda request: peer["uid"])
    mine, mine_tok = await _new(client, "claude-code:aaaa1111", ticket_role="owner")
    peer["uid"] = 1002
    theirs, theirs_tok = await _new(client, "claude-code:bbbb2222", ticket_role="owner")
    their_sender, their_sender_tok = await _new(client, "codex:cccc3333", ticket_role="reviewer")
    peer["uid"] = 1001
    my_sender, my_sender_tok = await _new(client, "codex:dddd4444", ticket_role="reviewer")
    mine_note = await client.post("/api/messages", json={
        "sender_session_id": my_sender["id"], "ticket_id": TICKET, "body": "mine"},
        headers=auth(my_sender_tok))
    peer["uid"] = 1002
    their_note = await client.post("/api/messages", json={
        "sender_session_id": their_sender["id"], "ticket_id": TICKET, "body": "theirs"},
        headers=auth(their_sender_tok))
    assert mine_note.json()["recipient_session_id"] == mine["id"]
    assert their_note.json()["recipient_session_id"] == theirs["id"]


# --- delegation children: the mode fixes the role ----------------------------------------

@pytest.mark.asyncio
async def test_a_delegated_child_role_is_fixed_by_its_mode(client):
    parent, _ = await _new(client, "claude-code:aaaa1111", ticket_role="owner")
    for mode, role in (("VERIFY", "reviewer"), ("READ_ONLY", "reviewer"),
                       ("IMPLEMENT", "contributor")):
        made = await client.post("/api/delegations", json={
            "parent_session_id": parent["id"], "delegated_worker": "codex", "mode": mode,
            "acceptance": "a verdict"})
        assert made.status_code == 201, made.text
        delegation = made.json()["id"]
        refused = await client.post("/api/sessions", json={
            "developer": "pvestal", "agent": "codex:delegate", "scope": [],
            "ticket_id": TICKET, "ticket_role": "owner", "delegation_id": delegation})
        assert refused.status_code == 409, refused.text
        assert refused.json()["detail"]["error"] == "role_fixed_by_delegation"
        scope = ["a.txt"] if role == "contributor" else []
        child = await client.post("/api/sessions", json={
            "developer": "pvestal", "agent": "codex:delegate", "scope": scope,
            "auto_lock": False, "ticket_id": TICKET, "delegation_id": delegation})
        assert child.status_code == 201, child.text
        assert child.json()["ticket_role"] == role
