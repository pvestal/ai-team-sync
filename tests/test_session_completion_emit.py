"""Session completion is authoritative; memory ingestion is support work.

A completed session releases locks and settles ownership. If the support layer
that turns it into durable knowledge is down, slow, or broken, none of that may
change -- the alternative is a finished session whose locks are held hostage by
an unrelated service. Since #3537 the event is recorded in the completion's own
transaction and delivered afterwards; a delivery failure is the outbox's problem,
never the caller's.
"""

from __future__ import annotations

import pytest

from ai_team_sync import completion_events as ce


async def _completed_session(client, **over):
    body = {"developer": "patrick", "agent": "claude-code:test", "scope": ["src/**"],
            "description": "work", "repo_root": "/opt/anime-studio", "auto_lock": True}
    body.update(over)
    created = await client.post("/api/sessions", json=body)
    assert created.status_code == 201
    sid = created.json()["id"]
    done = await client.patch(f"/api/sessions/{sid}",
                              json={"status": "completed", "summary": "a distinctive finding"},
                              headers={"X-ATS-Approval-Token": created.headers["X-ATS-Approval-Token"]})
    return sid, done


@pytest.mark.asyncio
async def test_completion_succeeds_when_delivery_explodes(client, monkeypatch):
    def explode():
        raise RuntimeError("delivery layer is on fire")
    monkeypatch.setattr(ce, "kick", explode)
    with pytest.raises(RuntimeError):
        ce.kick()
    monkeypatch.setattr(ce, "kick", lambda: None)
    sid, done = await _completed_session(client)
    assert done.status_code == 200
    assert done.json()["status"] == "completed"


@pytest.mark.asyncio
async def test_completion_releases_locks_regardless_of_delivery(client, monkeypatch):
    async def down(url, payload):
        raise ConnectionError("unreachable")
    monkeypatch.setattr(ce, "_post", down)
    sid, done = await _completed_session(client)
    assert done.status_code == 200
    locks = await client.get("/api/locks")
    assert all(lk["session_id"] != sid for lk in locks.json())


def test_kick_without_a_running_loop_is_a_no_op():
    ce.kick()  # must not raise outside an event loop
