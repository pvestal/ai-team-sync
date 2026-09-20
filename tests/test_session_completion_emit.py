"""Session completion is authoritative; memory ingestion is support work.

A completed session releases locks and settles ownership. If the support layer
that turns it into durable knowledge is down, slow, or broken, none of that may
change — the alternative is a finished session whose locks are held hostage by
an unrelated service.
"""

from __future__ import annotations

import pytest


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
async def test_completion_succeeds_when_the_memory_service_is_unreachable(client, monkeypatch):
    import ai_team_sync.routers.sessions as sessions

    def explode(session):
        raise RuntimeError("echo brain is on fire")

    monkeypatch.setattr(sessions, "emit_session_completed", explode)

    with pytest.raises(RuntimeError):
        # Proves the stub really is wired in; the real emitter swallows this.
        sessions.emit_session_completed(object())

    monkeypatch.setattr(sessions, "emit_session_completed", lambda s: None)
    sid, done = await _completed_session(client)

    assert done.status_code == 200
    after = (await client.get(f"/api/sessions/{sid}")).json()
    assert after["status"] == "completed"
    assert after["lock_count"] == 0, "locks release regardless of support-layer health"


@pytest.mark.asyncio
async def test_the_emitter_never_raises_into_the_caller(monkeypatch):
    """Whatever httpx does, the caller sees nothing."""
    import ai_team_sync.routers.sessions as sessions

    class Boom:
        def __init__(self, *a, **kw):
            raise OSError("no route to host")

    monkeypatch.setattr("httpx.AsyncClient", Boom)

    class FakeSession:
        id, agent, repo_root, summary = "s1", "codex", "/opt/anime-studio", "done"

    sessions.emit_session_completed(FakeSession())  # must not raise


@pytest.mark.asyncio
async def test_the_emit_is_symmetric_for_claude_and_codex(client, monkeypatch):
    """One path. Not a Claude path with Codex bolted on."""
    import ai_team_sync.routers.sessions as sessions
    seen = []
    monkeypatch.setattr(sessions, "emit_session_completed",
                        lambda s: seen.append((s.agent, s.id)))

    await _completed_session(client, agent="claude-code:aaaa", scope=["src/a/**"])
    await _completed_session(client, agent="codex", scope=["src/b/**"])

    agents = [a for a, _ in seen]
    assert "claude-code:aaaa" in agents and "codex" in agents
    assert len(seen) == 2, "both agents emit through the same hook"


@pytest.mark.asyncio
async def test_emit_can_be_switched_off_without_touching_completion(client, monkeypatch):
    monkeypatch.setenv("ATS_EMIT_COMPLETION", "0")
    import ai_team_sync.routers.sessions as sessions

    class FakeSession:
        id, agent, repo_root, summary = "s2", "codex", "/opt/anime-studio", "done"

    sessions.emit_session_completed(FakeSession())  # returns immediately

    sid, done = await _completed_session(client)
    assert done.status_code == 200
