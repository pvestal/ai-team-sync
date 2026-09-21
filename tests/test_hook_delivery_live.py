"""Exercise addressed delivery through real hook and MCP subprocesses on loopback."""

from __future__ import annotations

import json
import os
import re
import socket
import subprocess
import sys
import time
from pathlib import Path

import httpx
import pytest


ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / "src"


@pytest.fixture
def live_ats(tmp_path):
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    url = f"http://127.0.0.1:{port}"
    env = dict(os.environ, PYTHONPATH=str(SOURCE),
               DATABASE_URL=f"sqlite+aiosqlite:///{tmp_path / 'ats.db'}",
               SLACK_WEBHOOK_URL="", TELEGRAM_BOT_TOKEN="", TELEGRAM_CHAT_ID="")
    proc = subprocess.Popen(
        [sys.executable, "-c", "import uvicorn; from ai_team_sync.server import app; "
         f"uvicorn.run(app, host='127.0.0.1', port={port}, proxy_headers=False, log_level='error')"],
        cwd=tmp_path, env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        for _ in range(100):
            if proc.poll() is not None:
                pytest.fail("temporary ATS server exited during startup")
            try:
                if httpx.get(url + "/health", timeout=0.2).status_code == 200:
                    break
            except httpx.HTTPError:
                pass
            time.sleep(0.05)
        else:
            pytest.fail("temporary ATS server did not become healthy")
        with httpx.Client(base_url=url, timeout=5) as client:
            yield url, client, env
    finally:
        proc.terminate()
        proc.wait(timeout=5)


def _client_env(base, state, url, cid, agent="claude-code"):
    state.mkdir(mode=0o700)
    env = dict(base, ATS_STATE_DIR=str(state), ATS_SERVER_URL=url,
               CLAUDE_CODE_SESSION_ID=cid, ATS_AGENT=agent)
    env.pop("ATS_SESSION_ID", None)
    env.pop("ATS_SESSION", None)
    return env


def _hook(module, env, cid):
    result = subprocess.run(
        [sys.executable, "-m", module], input=json.dumps({"session_id": cid}),
        text=True, capture_output=True, env=env, timeout=10)
    assert result.returncode == 0, result.stderr
    return result.stdout


def _pointer(state, cid):
    return (state / f".ats_session_{cid[:8]}").read_text().strip()


def _token(state, cid):
    return json.loads((state / f".ats_approval_{cid[:8]}").read_text())["token"]


def _new_session(client, agent):
    result = client.post("/api/sessions", json={
        "developer": "pvestal", "agent": agent, "scope": []})
    assert result.status_code == 201, result.text
    return result.json()["id"], result.headers["X-ATS-Approval-Token"]


def _send(client, sender, token, recipient, body):
    result = client.post("/api/messages", json={
        "sender_session_id": sender, "recipient_session_id": recipient, "body": body},
        headers={"X-ATS-Approval-Token": token})
    assert result.status_code == 201, result.text
    return result.json()["id"]


def test_isolated_server_ticket_turnover_and_direct_authority(live_ats):
    """Fresh loopback server, real HTTP requests, one receipt per message."""
    _, client, _ = live_ats

    def ticket_session(agent, ticket=2907):
        made = client.post("/api/sessions", json={
            "developer": "pvestal", "agent": agent, "scope": [],
            "ticket_id": ticket})
        assert made.status_code == 201, made.text
        return made.json()["id"], made.headers["X-ATS-Approval-Token"]

    def headers(token):
        return {"X-ATS-Approval-Token": token}

    sender, sender_token = ticket_session("codex:sender")
    queued = client.post("/api/messages", json={
        "sender_session_id": sender, "ticket_id": 2907, "body": "ticket handoff",
    }, headers=headers(sender_token))
    assert queued.status_code == 201, queued.text
    ticket_mid = queued.json()["id"]
    first, first_token = ticket_session("claude-code:first")
    direct_mid = _send(client, sender, sender_token, first, "exact direct")
    first_inbox = client.get(f"/api/sessions/{first}/messages",
                             headers=headers(first_token)).json()
    assert [row["id"] for row in first_inbox if row["id"] in (ticket_mid, direct_mid)] == [
        ticket_mid, direct_mid]
    foreign, foreign_token = ticket_session("codex:foreign", ticket=9999)
    assert client.patch(f"/api/sessions/{first}", json={
        "status": "completed"}, headers=headers(foreign_token)).status_code == 403
    assert client.post(f"/api/messages/{direct_mid}/readdress", json={
        "sender_session_id": foreign, "recipient_session_id": sender,
    }, headers=headers(foreign_token)).status_code == 404
    assert client.patch(f"/api/sessions/{first}", json={
        "status": "completed"}, headers=headers(first_token)).status_code == 200
    second, second_token = ticket_session("claude-code:second")
    second_inbox = client.get(f"/api/sessions/{second}/messages",
                              headers=headers(second_token)).json()
    assert [row["id"] for row in second_inbox if row["id"] == ticket_mid] == [ticket_mid]
    assert direct_mid not in [row["id"] for row in second_inbox]
    moved = client.post(f"/api/messages/{direct_mid}/readdress", json={
        "sender_session_id": sender, "recipient_session_id": second,
    }, headers=headers(sender_token))
    assert moved.status_code == 200, moved.text
    assert moved.json()["original_recipient_session_id"] == first
    assert moved.json()["sender_session_id"] == sender
    for mid in (ticket_mid, direct_mid):
        ack = client.post(f"/api/messages/{mid}/acknowledge", json={
            "recipient_session_id": second}, headers=headers(second_token))
        assert ack.status_code == 200 and ack.json()["acknowledged_at"]
        again = client.post(f"/api/messages/{mid}/acknowledge", json={
            "recipient_session_id": second}, headers=headers(second_token))
        assert again.json()["acknowledged_at"] == ack.json()["acknowledged_at"]
        status = client.get(f"/api/messages/{mid}", params={
            "sender_session_id": sender}, headers=headers(sender_token))
        assert status.json()["acknowledged_at"] == ack.json()["acknowledged_at"]
        assert status.json()["sender_session_id"] == sender


def test_claude_hook_exact_delivery_and_ack(live_ats, tmp_path):
    url, client, base = live_ats
    sender, sender_token = _new_session(client, "codex:sender")
    claude_cid = "aaaaaaaa-0000-0000-0000-000000000001"
    other_cid = "bbbbbbbb-0000-0000-0000-000000000002"
    claude_state = tmp_path / "claude-state"
    other_state = tmp_path / "other-state"
    claude_env = _client_env(base, claude_state, url, claude_cid)
    other_env = _client_env(base, other_state, url, other_cid)
    _hook("ai_team_sync.hooks.session_autostart", claude_env, claude_cid)
    _hook("ai_team_sync.hooks.session_autostart", other_env, other_cid)
    recipient = _pointer(claude_state, claude_cid)
    other = _pointer(other_state, other_cid)
    token = _token(claude_state, claude_cid)
    other_token = _token(other_state, other_cid)
    assert _hook("ai_team_sync.hooks.override_inbox", claude_env, claude_cid) == ""

    message_id = _send(client, sender, sender_token, recipient, "Read the exact target inbox")
    shown = _hook("ai_team_sync.hooks.override_inbox", claude_env, claude_cid)
    assert message_id in shown and "Read the exact target inbox" in shown
    assert "acknowledge_message" in shown
    assert message_id in _hook("ai_team_sync.hooks.override_inbox", claude_env, claude_cid)
    assert _hook("ai_team_sync.hooks.override_inbox", other_env, other_cid) == ""
    assert client.get(f"/api/sessions/{recipient}/messages", headers={
        "X-ATS-Approval-Token": other_token}).status_code == 403
    assert client.post(f"/api/messages/{message_id}/acknowledge", json={
        "recipient_session_id": other}, headers={
        "X-ATS-Approval-Token": other_token}).status_code == 404

    ack = client.post(f"/api/messages/{message_id}/acknowledge", json={
        "recipient_session_id": recipient}, headers={"X-ATS-Approval-Token": token})
    assert ack.status_code == 200 and ack.json()["acknowledged_at"]
    status = client.get(f"/api/messages/{message_id}", params={
        "sender_session_id": sender}, headers={"X-ATS-Approval-Token": sender_token})
    assert status.json()["acknowledged_at"] == ack.json()["acknowledged_at"]
    assert _hook("ai_team_sync.hooks.override_inbox", claude_env, claude_cid) == ""


def test_stale_claude_session_warns_and_needs_new_address(live_ats, tmp_path):
    url, client, base = live_ats
    sender, sender_token = _new_session(client, "codex:sender")
    old_cid = "cccccccc-0000-0000-0000-000000000003"
    state = tmp_path / "stale-state"
    env = _client_env(base, state, url, old_cid)
    stale, stale_token = _new_session(client, "claude-code:cccccccc")
    (state / ".ats_session_cccccccc").write_text(stale)
    message_id = _send(client, sender, sender_token, stale, "old exact recipient")
    warning = _hook("ai_team_sync.hooks.override_inbox", env, old_cid)
    assert "no saved session capability" in warning
    assert message_id not in warning
    # SessionStart refires do not recover a secret that the old client discarded.
    _hook("ai_team_sync.hooks.session_autostart", env, old_cid)
    assert _pointer(state, old_cid) == stale
    assert not (state / ".ats_approval_cccccccc").exists()

    completed = client.post(f"/api/sessions/{stale}/complete", json={
        "summary": "old client finished at safe pause"}, headers={
            "X-ATS-Approval-Token": stale_token})
    assert completed.status_code == 200
    new_cid = "dddddddd-0000-0000-0000-000000000004"
    fresh_env = _client_env(base, tmp_path / "fresh-state", url, new_cid)
    _hook("ai_team_sync.hooks.session_autostart", fresh_env, new_cid)
    fresh = _pointer(tmp_path / "fresh-state", new_cid)
    assert fresh != stale
    assert _hook("ai_team_sync.hooks.override_inbox", fresh_env, new_cid) == ""
    replacement = _send(client, sender, sender_token, fresh, "resent to fresh recipient")
    shown = _hook("ai_team_sync.hooks.override_inbox", fresh_env, new_cid)
    assert replacement in shown and message_id not in shown
    old_status = client.get(f"/api/messages/{message_id}", params={
        "sender_session_id": sender}, headers={"X-ATS-Approval-Token": sender_token})
    assert old_status.json()["acknowledged_at"] is None


def test_ats_session_env_is_client_key_not_ats_row_id(live_ats, tmp_path):
    url, client, base = live_ats
    sender, sender_token = _new_session(client, "codex:sender")
    cid = "eeeeeeee-0000-0000-0000-000000000005"
    state = tmp_path / "fallback-state"
    env = _client_env(base, state, url, cid)
    _hook("ai_team_sync.hooks.session_autostart", env, cid)
    recipient = _pointer(state, cid)
    message_id = _send(client, sender, sender_token, recipient, "ATS_SESSION fallback")
    env.pop("CLAUDE_CODE_SESSION_ID")
    env["ATS_SESSION"] = cid
    assert message_id in _hook("ai_team_sync.hooks.override_inbox", env, "")


class McpClient:
    def __init__(self, env):
        self.process = subprocess.Popen(
            [sys.executable, "-m", "ai_team_sync.mcp.server"], stdin=subprocess.PIPE,
            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True, env=env)
        self.counter = 0
        self.request("initialize", {"protocolVersion": "2024-11-05", "capabilities": {},
                    "clientInfo": {"name": "live-delivery-regression", "version": "1"}})
        self.process.stdin.write(json.dumps({"jsonrpc": "2.0",
                                             "method": "notifications/initialized"}) + "\n")
        self.process.stdin.flush()

    def request(self, method, params):
        self.counter += 1
        self.process.stdin.write(json.dumps({"jsonrpc": "2.0", "id": self.counter,
                                             "method": method, "params": params}) + "\n")
        self.process.stdin.flush()
        result = json.loads(self.process.stdout.readline())
        assert "error" not in result, result
        return result["result"]

    def tool(self, name, arguments=None):
        result = self.request("tools/call", {"name": name,
                                              "arguments": arguments or {}})
        return "\n".join(part.get("text", "") for part in result["content"])

    def close(self):
        self.process.terminate()
        self.process.wait(timeout=5)


def test_fresh_codex_mcp_nudge_and_ack(live_ats, tmp_path):
    url, client, base = live_ats
    sender, sender_token = _new_session(client, "claude-code:sender")
    state = tmp_path / "codex-state"
    env = _client_env(base, state, url, "fffffff0-0000-0000-0000-000000000006",
                      agent="codex")
    mcp = McpClient(env)
    try:
        start = mcp.tool("start_session", {"scope": [],
                         "description": "fresh Codex message delivery probe"})
        assert "Session ID:" in start, start
        recipient = _pointer(state, "fffffff0-0000-0000-0000-000000000006")
        message_id = _send(client, sender, sender_token, recipient, "Codex MCP target")
        nudge = mcp.tool("team_status")
        assert message_id in nudge and "Codex MCP target" in nudge
        assert message_id in mcp.tool("message_inbox")
        assert "acknowledged" in mcp.tool("acknowledge_message", {"message_id": message_id})
        status = client.get(f"/api/messages/{message_id}", params={
            "sender_session_id": sender}, headers={"X-ATS-Approval-Token": sender_token})
        assert status.json()["acknowledged_at"]
        assert message_id not in mcp.tool("message_inbox")
    finally:
        mcp.close()


def test_mcp_placeholder_adoption_authorizes_and_reports_refusal(
        live_ats, tmp_path, monkeypatch):
    """The real stdio caller authenticates the old row and never hides a 403."""
    url, client, base = live_ats

    def start_from_placeholder(cid, state_name, corrupt_capability=False):
        state = tmp_path / state_name
        env = _client_env(base, state, url, cid, agent="claude-code")
        _hook("ai_team_sync.hooks.session_autostart", env, cid)
        placeholder = _pointer(state, cid)
        if corrupt_capability:
            # Force the real ATS capability door to reject the adoption PATCH.
            from ai_team_sync import session_pointer as sp
            monkeypatch.setenv("ATS_STATE_DIR", str(state))
            sp.save_approval_token(
                placeholder, "not-the-placeholder-capability", cid=cid)

        mcp = McpClient(env)
        try:
            output = mcp.tool("start_session", {
                "scope": [], "description": "replace SessionStart placeholder",
            })
        finally:
            mcp.close()
        row = client.get(f"/api/sessions/{placeholder}")
        assert row.status_code == 200, row.text
        return placeholder, row.json(), output

    accepted_id, accepted, accepted_output = start_from_placeholder(
        "abc00001-0000-0000-0000-000000000001", "adopt-accepted")
    assert accepted["status"] == "completed", (
        "the real server accepts only the placeholder's own capability")
    assert "auto-registered placeholder session completed: 1" in accepted_output

    rejected_id, rejected, rejected_output = start_from_placeholder(
        "abc00002-0000-0000-0000-000000000002", "adopt-rejected",
        corrupt_capability=True)
    assert rejected["status"] == "active", "the real server must reject the bad token"
    assert "HTTP 403" in rejected_output and rejected_id[:8] in rejected_output
    assert "auto-registered placeholder session completed: 1" not in rejected_output
    assert "Session ID:" in rejected_output, "best-effort adoption must not block start"


def test_fresh_mcp_sender_readdresses_to_new_claude_hook(live_ats, tmp_path):
    url, client, base = live_ats
    sender_cid = "fffffff1-0000-0000-0000-000000000007"
    sender_state = tmp_path / "sender-state"
    sender_env = _client_env(base, sender_state, url, sender_cid, agent="codex")
    old_cid = "aaaaaaa1-0000-0000-0000-000000000008"
    new_cid = "aaaaaaa2-0000-0000-0000-000000000009"
    old_state, new_state = tmp_path / "old-state", tmp_path / "new-state"
    old_env = _client_env(base, old_state, url, old_cid)
    new_env = _client_env(base, new_state, url, new_cid)
    mcp = McpClient(sender_env)
    try:
        assert "Session ID:" in mcp.tool("start_session", {
            "scope": [], "description": "turnover sender"})
        _hook("ai_team_sync.hooks.session_autostart", old_env, old_cid)
        old = _pointer(old_state, old_cid)
        sent = mcp.tool("send_message", {
            "recipient_session_id": old, "body": "survive exact-session turnover"})
        message_id = re.search(r"Message ID: ([a-f0-9-]{36})", sent).group(1)
        assert client.post(f"/api/sessions/{old}/complete", json={
            "summary": "ended unread"}, headers={
                "X-ATS-Approval-Token": _token(old_state, old_cid)}).status_code == 200
        _hook("ai_team_sync.hooks.session_autostart", new_env, new_cid)
        new = _pointer(new_state, new_cid)
        assert message_id not in _hook("ai_team_sync.hooks.override_inbox", new_env,
                                       new_cid)
        moved = mcp.tool("readdress_message", {
            "message_id": message_id, "recipient_session_id": new})
        assert message_id in moved and old in moved and new in moved
        assert message_id in _hook("ai_team_sync.hooks.override_inbox", new_env,
                                    new_cid)
        ack = client.post(f"/api/messages/{message_id}/acknowledge", json={
            "recipient_session_id": new}, headers={
                "X-ATS-Approval-Token": _token(new_state, new_cid)})
        assert ack.status_code == 200, ack.text
        assert "acknowledged" in mcp.tool("message_status", {"message_id": message_id})
    finally:
        mcp.close()
