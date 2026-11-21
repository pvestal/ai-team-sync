"""#3774 ATS side: ask_operator payload, the per-session open-question list, answer surfacing."""
from __future__ import annotations

import stat

import pytest

from ai_team_sync import operator_questions as oq

SID = "0edffe40-52eb-4029-93bd-1848a6c0ae9c"
Q1 = "0f8fad5b-d9cb-469f-a165-70867728950e"
Q2 = "7c9e6679-7425-40de-944b-e07fc1f90ae7"


@pytest.fixture(autouse=True)
def state(tmp_path, monkeypatch):
    monkeypatch.setenv("ATS_STATE_DIR", str(tmp_path))
    return tmp_path


def test_payload_always_carries_the_callers_own_session():
    body = oq.ask_payload(SID, {"tower_task_id": 3774, "question_key": "k", "question": "q?",
                                "ats_session_id": "someone-else", "session_id": "other"})
    assert body["ats_session_id"] == SID and "session_id" not in body


def test_pending_list_is_private_and_round_trips(state):
    assert oq.pending_ids(SID) == []
    oq.remember(SID, Q1)
    oq.remember(SID, Q2)
    oq.remember(SID, Q1)
    assert oq.pending_ids(SID) == sorted([Q1, Q2])
    p = state / ".ats_operator_questions" / f"{SID}.json"
    assert stat.S_IMODE(p.stat().st_mode) == 0o600
    oq.forget(SID, Q1)
    oq.forget(SID, Q2)
    assert not p.exists()
    with pytest.raises(ValueError):
        oq.remember("../escape", Q1)


def test_ask_result_wording():
    ok = oq.format_ask_result(200, {"ok": True, "question_id": Q1, "delivery": "now", "sent": {"ok": True}})
    assert "sent to the operator's Telegram now" in ok
    q = oq.format_ask_result(200, {"ok": True, "question_id": Q1, "delivery": "digest"})
    assert "morning digest" in q
    ruled = oq.format_ask_result(409, {"detail": {"ok": False, "error": "ALREADY_RULED", "ruling": "Answer: Lift.",
                                                  "decided_at": "2026-11-21T23:59:00+00:00"}})
    assert ruled.startswith("⛔ Not asked") and "Answer: Lift." in ruled
    assert "ASKER_NOT_ACTIVE" in oq.format_ask_result(409, {"detail": {"error": "ASKER_NOT_ACTIVE", "why": "x"}})


class Resp:
    def __init__(self, code, data=None):
        self.status_code, self._data = code, data

    def json(self):
        return self._data


class Client:
    def __init__(self, answers, delivered_code=200):
        self.answers, self.delivered_code, self.calls = answers, delivered_code, []

    def get(self, url, params=None):
        self.calls.append(("get", url, params))
        return Resp(200, {"ok": True, "answers": self.answers})

    def post(self, url, json=None):
        self.calls.append(("post", url, json))
        return Resp(self.delivered_code, {"ok": True, "marked": True})


ANSWER = {"question_id": Q1, "tower_task_id": 3774, "question_key": "forest-hold",
          "question": "Keep or lift the Forest hold?", "answer": "Lift the hold", "ruling": "",
          "answered_at": "2026-11-21T23:59:00+00:00", "decision_id": "d-1"}


def test_hook_calls_nothing_without_an_open_question():
    c = Client([ANSWER])
    assert oq.surface_answers(SID, c) is None and c.calls == []


def test_hook_surfaces_own_answers_once_and_marks_them_delivered():
    oq.remember(SID, Q1)
    stranger = dict(ANSWER, question_id=Q2)          # not asked by this session's list
    c = Client([ANSWER, stranger])
    note = oq.surface_answers(SID, c)
    assert note.startswith("OPERATOR RULING via Telegram (1)")
    assert '#3774 forest-hold: "Keep or lift the Forest hold?" -> Lift the hold' in note
    assert [x[0] for x in c.calls] == ["get", "post"] and c.calls[1][2] == {"ats_session_id": SID}
    assert oq.pending_ids(SID) == []


def test_undelivered_mark_keeps_the_question_for_the_next_prompt():
    oq.remember(SID, Q1)
    assert oq.surface_answers(SID, Client([ANSWER], delivered_code=500))
    assert oq.pending_ids(SID) == [Q1]
