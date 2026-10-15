"""No ATS test can reach the live Echo Brain (#3537 defect 1).

The leak was through inherited environment: an in-process app and every spawned
ats-server read ECHO_BRAIN_URL, defaulting to the production localhost:8309.
These tests start a REAL ats-server subprocess on a file database (the shape the
in-memory guard never covered) and complete a real session with a handoff.
"""

from __future__ import annotations

import os
import socket
import subprocess
import sys
import time
from pathlib import Path

import httpx
import pytest

SOURCE = Path(__file__).resolve().parents[1] / "src"
PRODUCTION_ECHO_PORT = "8309"


def _spawn(tmp_path, **overrides):
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    env = dict(os.environ, PYTHONPATH=str(SOURCE),
               DATABASE_URL=f"sqlite+aiosqlite:///{tmp_path / 'ats.db'}",
               SLACK_WEBHOOK_URL="", TELEGRAM_BOT_TOKEN="", TELEGRAM_CHAT_ID="")
    env.update(overrides)
    proc = subprocess.Popen(
        [sys.executable, "-c", "import uvicorn; from ai_team_sync.server import app; "
         f"uvicorn.run(app, host='127.0.0.1', port={port}, log_level='error')"],
        cwd=tmp_path, env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    url = f"http://127.0.0.1:{port}"
    for _ in range(200):
        if proc.poll() is not None:
            pytest.fail("ats-server exited during startup")
        try:
            if httpx.get(url + "/health", timeout=0.2).status_code == 200:
                return proc, url
        except httpx.HTTPError:
            pass
        time.sleep(0.05)
    proc.terminate()
    pytest.fail("ats-server did not become healthy")


def _complete_with_handoff(url: str) -> str:
    with httpx.Client(base_url=url, timeout=5) as client:
        made = client.post("/api/sessions", json={
            "developer": "pvestal", "agent": "claude-code:iso", "scope": [],
            "ticket_id": 9903537, "ticket_role": "owner"})
        assert made.status_code == 201, made.text
        sid, token = made.json()["id"], made.headers["X-ATS-Approval-Token"]
        done = client.patch(f"/api/sessions/{sid}", json={
            "status": "completed", "handoff": {"verdict": "isolation probe"}},
            headers={"X-ATS-Approval-Token": token})
        assert done.status_code == 200, done.text
    return sid


def _wait_for(predicate, seconds: float) -> bool:
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.1)
    return predicate()


def test_the_run_points_echo_at_the_tripwire(echo_tripwire):
    url = os.environ["ECHO_BRAIN_URL"]
    assert url == echo_tripwire.url
    assert f":{PRODUCTION_ECHO_PORT}" not in url
    assert os.environ["ATS_EMIT_COMPLETION"] == "0"


def test_spawned_server_completion_reaches_only_the_tripwire(tmp_path, echo_tripwire):
    """Positive control: with delivery explicitly ON, a spawned server's event
    lands at the tripwire -- so the URL it inherited is the tripwire, not Echo."""
    proc, url = _spawn(tmp_path, ATS_EMIT_COMPLETION="1")
    try:
        sid = _complete_with_handoff(url)
        assert _wait_for(lambda: any(sid in r["body"]
                                     for r in echo_tripwire.completion_events()), 45), \
            "a delivering server's completion never reached the tripwire"
    finally:
        proc.terminate()
        proc.wait(timeout=10)


def test_spawned_server_sends_nothing_by_default(tmp_path, echo_tripwire):
    """Negative control: the inherited default sends no event anywhere."""
    before = echo_tripwire.count()
    proc, url = _spawn(tmp_path)
    try:
        sid = _complete_with_handoff(url)
        time.sleep(3)
        assert not [r for r in echo_tripwire.completion_events(since=before)
                    if sid in r["body"]]
    finally:
        proc.terminate()
        proc.wait(timeout=10)
