"""#2517 failure 2: the RESTful-looking nested paths agents guess were 404s.

POST /api/sessions/{id}/complete and POST /api/sessions/{id}/decisions both
returned {"detail":"Not Found"}; an agent that guessed them concluded ATS was
unavailable and proceeded WITHOUT coordinating. The aliases delegate to the
real surface (PATCH /api/sessions/{id} and POST /api/decisions) so semantics
stay single-sourced.
"""
import pytest


async def _mk_session(client):
    r = await client.post("/api/sessions", json={
        "developer": "d", "agent": "claude-code:5ead4223",
        "scope": ["a/*"], "description": "t", "auto_lock": False,
    })
    assert r.status_code == 201, r.text
    return r.json()["id"]


@pytest.mark.asyncio
async def test_nested_complete_completes_and_releases(client):
    sid = await _mk_session(client)
    r = await client.post(f"/api/sessions/{sid}/complete",
                          json={"summary": "done via nested alias"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["status"] == "completed"
    assert body["summary"] == "done via nested alias"


@pytest.mark.asyncio
async def test_nested_complete_empty_body(client):
    sid = await _mk_session(client)
    r = await client.post(f"/api/sessions/{sid}/complete")
    assert r.status_code == 200, r.text
    assert r.json()["status"] == "completed"


@pytest.mark.asyncio
async def test_nested_decision_lands_on_the_session(client):
    sid = await _mk_session(client)
    r = await client.post(f"/api/sessions/{sid}/decisions",
                          json={"title": "T", "chosen": "A", "rejected": "B"})
    assert r.status_code == 201, r.text
    assert r.json()["session_id"] == sid
    # and it is readable through the real surface
    r = await client.get("/api/decisions", params={"session_id": sid})
    if r.status_code == 200:  # list endpoint exists
        assert any(d["title"] == "T" for d in r.json())


@pytest.mark.asyncio
async def test_nested_decision_path_wins_over_body(client):
    sid = await _mk_session(client)
    r = await client.post(f"/api/sessions/{sid}/decisions",
                          json={"title": "T2", "chosen": "A",
                                "session_id": "someone-else"})
    assert r.status_code == 201, r.text
    assert r.json()["session_id"] == sid


@pytest.mark.asyncio
async def test_nested_complete_unknown_session_404(client):
    r = await client.post("/api/sessions/nope/complete")
    assert r.status_code == 404
