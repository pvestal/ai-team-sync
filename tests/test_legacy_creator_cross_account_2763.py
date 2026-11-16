"""#2763: a session whose creator was never identified belongs to nobody who can prove it.

cross_account() used to fall through, for creator_uid NULL, to "an identified,
non-bound account may" -- so another OS account could heartbeat a REAPED legacy
row back to active (verified by probe in the #2760 review). Measured 2026-11-15
on the live ATS database before choosing the fix: 1680 NULL-creator rows, all
completed (199 reaped), none holding a lock or a restoration journal, newest
started 2026-09-13. Option (b): every requester is foreign; the rows stay
readable history.
"""

from __future__ import annotations

import os

import pytest

from ai_team_sync import peer_identity
from ai_team_sync.background_tasks import auto_complete_stale_sessions
from ai_team_sync.models import Session
from ai_team_sync.routers.locks import cross_account

from .test_resurrection_restores_locks_2760 import _heartbeat, _row, _silent_session

FOREIGN_UID = os.getuid() + 4242


def _as(monkeypatch, uid):
    monkeypatch.setattr(peer_identity, "peer_uid_for_request", lambda request: uid)


@pytest.mark.parametrize("status", ["active", "paused", "completed"])
@pytest.mark.parametrize("peer", [os.getuid(), FOREIGN_UID, None])
def test_a_null_creator_is_foreign_to_every_requester(status, peer):
    legacy = Session(developer="d", agent="a", status=status, creator_uid=None)
    assert cross_account(peer, legacy, any_status=True) is True
    if status != "completed":
        assert cross_account(peer, legacy) is True


def test_an_identified_creator_is_unchanged():
    row = Session(developer="d", agent="a", status="active", creator_uid=os.getuid())
    assert cross_account(os.getuid(), row) is False
    assert cross_account(FOREIGN_UID, row) is True
    assert cross_account(None, row) is True
    done = Session(developer="d", agent="a", status="completed", creator_uid=FOREIGN_UID)
    assert cross_account(os.getuid(), done) is False, "not live, any_status=False"
    assert cross_account(os.getuid(), done, any_status=True) is True


async def test_another_account_cannot_resurrect_a_reaped_legacy_row(
        client, db_session, monkeypatch):
    sess = await _silent_session(db_session, scope=["src/**"], identified=False)
    sid = sess.id
    assert await auto_complete_stale_sessions(db_session) == 1
    _as(monkeypatch, FOREIGN_UID)

    resp = await _heartbeat(client, sid)
    assert resp.status_code == 403, resp.text
    assert resp.json()["detail"]["error"] == "session_not_yours"
    row = await _row(db_session, sid)
    assert row.status == "completed" and row.auto_completed, "the board was not changed"


async def test_no_account_can_revive_or_change_a_legacy_row_but_it_stays_readable(
        client, db_session, monkeypatch):
    sess = await _silent_session(db_session, scope=["src/**"], identified=False)
    sid = sess.id
    assert await auto_complete_stale_sessions(db_session) == 1
    _as(monkeypatch, os.getuid())  # even the account ATS runs as cannot prove ownership

    assert (await _heartbeat(client, sid)).status_code == 403
    patched = await client.patch(f"/api/sessions/{sid}", json={"summary": "rewritten"})
    assert patched.status_code == 403, patched.text
    row = await _row(db_session, sid)
    assert row.status == "completed" and (row.summary or "") != "rewritten"

    got = await client.get(f"/api/sessions/{sid}")
    assert got.status_code == 200 and got.json()["status"] == "completed"
    assert sid in {s["id"] for s in (await client.get("/api/sessions")).json()}


async def test_an_identified_owner_still_resurrects_its_own_reaped_row(
        client, db_session, monkeypatch):
    sess = await _silent_session(db_session, scope=["src/**"], identified=True)
    sid = sess.id
    assert await auto_complete_stale_sessions(db_session) == 1
    _as(monkeypatch, FOREIGN_UID)
    assert (await _heartbeat(client, sid)).status_code == 403
    _as(monkeypatch, os.getuid())
    resp = await _heartbeat(client, sid)
    assert resp.status_code == 200 and resp.json()["status"] == "active", resp.text
