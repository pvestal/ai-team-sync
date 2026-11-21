"""#3774 ATS side: ask_operator, the per-session question file, answer surfacing in the hook.

Codex r1 (BLOCK) asked for: the real MCP authorization path, a forged hook identity, info-only
wording, and answers acknowledged only after they were printed. Each has a test below.
"""
from __future__ import annotations

import io
import json
import stat
import sys

import pytest

import ai_team_sync.mcp.server as mcp
from ai_team_sync import operator_questions as oq
from tests.test_hook_delivery_live import live_ats  # noqa: F401  (fixture used by wired)
from tests.test_mcp_multi_session_capability_3593 import _start, wired  # noqa: F401

SID = "0edffe40-52eb-4029-93bd-1848a6c0ae9c"
Q1 = "0f8fad5b-d9cb-469f-a165-70867728950e"
Q2 = "7c9e6679-7425-40de-944b-e07fc1f90ae7"
T1, T2 = "a" * 48, "b" * 48


@pytest.fixture
def state(tmp_path, monkeypatch):
    monkeypatch.setenv("ATS_STATE_DIR", str(tmp_path))
    return tmp_path


# ---------------------------------------------------------------- pure helpers

def test_payload_always_carries_the_callers_own_session():
    body = oq.ask_payload(SID, {"tower_task_id": 3774, "question_key": "k", "question": "q?",
                                "ats_session_id": "someone-else", "session_id": "other"})
    assert body["ats_session_id"] == SID and "session_id" not in body


def test_question_file_is_private_and_round_trips(state):
    assert oq.pending_ids(SID) == []
    oq.remember(SID, Q1, T1)
    oq.remember(SID, Q2, T2)
    assert oq.pending_ids(SID) == sorted([Q1, Q2])
    p = state / ".ats_operator_questions" / f"{SID}.json"
    assert stat.S_IMODE(p.stat().st_mode) == 0o600
    assert stat.S_IMODE(p.parent.stat().st_mode) == 0o700
    oq.forget(SID, Q1)
    oq.forget(SID, Q2)
    assert not p.exists()
    with pytest.raises(ValueError):
        oq.remember("../escape", Q1, T1)
    with pytest.raises(ValueError):
        oq.remember(SID, Q1, "not-a-token")


def test_ask_result_wording_including_info_only():
    ok = oq.format_ask_result(200, {"ok": True, "question_id": Q1, "delivery": "now", "sent": {"ok": True}})
    assert "sent to the operator's Telegram now" in ok and "recorded as a ruling" in ok
    info = oq.format_ask_result(200, {"ok": True, "question_id": Q1, "delivery": "now", "sent": {"ok": True}},
                                info_only=True)
    assert "Notice" in info and "nothing is recorded" in info and "ruling" not in info
    assert "morning digest" in oq.format_ask_result(200, {"ok": True, "question_id": Q1, "delivery": "digest"})
    ruled = oq.format_ask_result(409, {"detail": {"ok": False, "error": "ALREADY_RULED", "ruling": "Answer: Lift.",
                                                  "decided_at": "2026-11-21T23:59:00+00:00"}})
    assert ruled.startswith("⛔ Not asked") and "Answer: Lift." in ruled


# ---------------------------------------------------------------- the MCP path

@pytest.fixture
def echo(wired, monkeypatch):  # noqa: F811  (pytest fixture parameter)
    """Route Echo calls to a recorder; everything else still goes to the in-process ATS app."""
    calls = []
    inner = mcp.httpx.AsyncClient

    class Resp:
        def __init__(self, code, data):
            self.status_code, self._d = code, data
            self.text = json.dumps(data)

        def json(self):
            return self._d

    class Client:
        def __init__(self, *a, **k):
            self._cm = inner(*a, **k)

        async def __aenter__(self):
            self._c = await self._cm.__aenter__()
            return self

        async def __aexit__(self, *exc):
            return await self._cm.__aexit__(*exc)

        async def post(self, url, *a, **k):
            if str(url).startswith(oq.ECHO_URL):
                calls.append(k.get("json"))
                info = (k.get("json") or {}).get("info_only")
                return Resp(200, {"ok": True, "question_id": Q1, "delivery": "now", "sent": {"ok": True},
                                  "delivery_token": T1, "info_only": info})
            return await self._c.post(url, *a, **k)

        def __getattr__(self, name):
            return getattr(self._c, name)
    monkeypatch.setattr(mcp.httpx, "AsyncClient", Client)
    return calls


ASK = {"tower_task_id": 3774, "question_key": "forest-hold", "question": "Keep or lift?",
       "options": [{"label": "Keep"}, {"label": "Lift"}]}


@pytest.mark.asyncio
async def test_ask_operator_files_as_the_callers_own_session_only(echo, tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    sid = await _start(3774, str(repo), "#3774 asker")
    text = (await mcp.call_tool("ask_operator", {**ASK, "ats_session_id": "forged"}))[0].text
    assert "filed" in text, text
    assert echo[-1]["ats_session_id"] == sid                    # never the caller-supplied id
    assert oq.pending_ids(sid) == [Q1]
    # a session this process never opened cannot be named
    n = len(echo)
    refused = (await mcp.call_tool("ask_operator", {**ASK, "session_id": "11111111-2222-4333-8444-555555555555"}))[0].text
    assert "filed" not in refused and len(echo) == n, refused


@pytest.mark.asyncio
async def test_info_only_is_reported_as_a_notice_and_not_awaited(echo, tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    sid = await _start(3774, str(repo), "#3774 notice")
    text = (await mcp.call_tool("ask_operator", {"tower_task_id": 3774, "question_key": "fyi",
                                                 "question": "Nothing to widen.", "info_only": True}))[0].text
    assert "Notice" in text and "no answer comes back" in text, text
    assert oq.pending_ids(sid) == []


# ---------------------------------------------------------------- the hook

ANSWER = {"ok": True, "answered": True, "question_id": Q1, "tower_task_id": 3774,
          "question_key": "forest-hold", "question": "Keep or lift the Forest hold?", "answer": "Lift",
          "ruling": "", "answered_at": "2026-11-21T23:59:00+00:00", "decision_id": "d-1"}


class SyncResp:
    def __init__(self, code, data):
        self.status_code, self._d = code, data

    def json(self):
        return self._d


def _run_hook(monkeypatch, *, token, events, delivered_code=200):
    import httpx
    from ai_team_sync import session_pointer as sp
    from ai_team_sync.hooks import override_inbox as hook

    class Client:
        def __init__(self, *a, **k):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def get(self, url, params=None, headers=None):
            events.append(("GET", url, (headers or {}).get(oq.TOKEN_HEADER)))
            if url.startswith(oq.ECHO_URL):
                return SyncResp(200, ANSWER if url.endswith(f"/{Q1}/answer") else {"ok": True, "answered": False})
            if "/override-requests" in url:
                return SyncResp(200, [])
            if url.endswith("/messages"):
                return SyncResp(200, [])
            return SyncResp(200, {"status": "active"})

        def post(self, url, json=None, headers=None):
            events.append(("POST", url, (headers or {}).get(oq.TOKEN_HEADER)))
            return SyncResp(delivered_code, {"ok": True, "marked": True})

    class Out(io.StringIO):
        def write(self, s):
            if s.strip():
                events.append(("PRINT", s.strip()[:40], None))
            return super().write(s)

    monkeypatch.setattr(httpx, "Client", Client)
    monkeypatch.setattr(sp, "load_approval_token", lambda sid, cid=None: token)
    monkeypatch.setenv("ATS_SESSION_ID", SID)
    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps({"session_id": "cid-1"})))
    monkeypatch.setattr(sys, "stdout", Out())
    with pytest.raises(SystemExit):
        hook.main()


def test_hook_without_this_sessions_capability_reads_no_answer(state, monkeypatch):
    oq.remember(SID, Q1, T1)                     # the victim's file exists on disk
    events = []
    _run_hook(monkeypatch, token="", events=events)   # forged/stale ATS_SESSION_ID: no capability
    assert not [e for e in events if e[1].startswith(oq.ECHO_URL)]
    assert not [e for e in events if e[0] == "PRINT" and e[1].startswith("OPERATOR RULING")]
    assert oq.pending_ids(SID) == [Q1]


def test_hook_prints_before_acknowledging_and_uses_the_questions_own_token(state, monkeypatch):
    oq.remember(SID, Q1, T1)
    oq.remember(SID, Q2, T2)
    events = []
    _run_hook(monkeypatch, token="capability", events=events)
    echo = [e for e in events if e[0] == "PRINT" or e[1].startswith(oq.ECHO_URL)]
    kinds = [(e[0], e[1].rsplit("/", 1)[-1] if e[0] != "PRINT" else "") for e in echo]
    assert ("PRINT", "") in kinds
    printed = kinds.index(("PRINT", ""))
    acked = [i for i, k in enumerate(kinds) if k == ("POST", "delivered")]
    assert acked and all(i > printed for i in acked)            # print first, then acknowledge
    tokens = {e[1].split("/")[-2]: e[2] for e in echo if e[0] == "GET"}
    assert tokens == {Q1: T1, Q2: T2}                           # each read with its own secret
    assert oq.pending_ids(SID) == [Q2]                          # Q2 unanswered: still open


def test_a_failed_acknowledgement_shows_the_answer_again_next_prompt(state, monkeypatch):
    oq.remember(SID, Q1, T1)
    _run_hook(monkeypatch, token="capability", events=[], delivered_code=500)
    assert oq.pending_ids(SID) == [Q1]
