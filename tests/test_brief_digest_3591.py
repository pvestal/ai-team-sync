"""#3591 context delivery: the prompt hook's note must put what controls the work
where the agent actually reads it.

Claude Code shows hook output inline only up to ~10 KB, and only the first 2 KB
beyond that; Codex truncates near the same size. Measured briefs were 14-50 KB,
so rulings, later named tickets and branches sat in an overflow file. The note
now leads with a priority digest built from the brief's structured fields and
stays inside a fixed budget, with an explicit pointer to the full brief.
"""
from __future__ import annotations

import pytest

from ai_team_sync import briefs
from ai_team_sync.hooks import ats_context
from tests.test_work_admission_3591 import governed  # noqa: F401  (fixture)


def _envelope(tid: int, ruling: str | None = None) -> dict:
    current = ([{"id": f"r{tid}", "effect": "BLOCK", "prohibition": True, "ruling": ruling}]
               if ruling else [])
    return {"id": tid, "title": f"title of {tid}", "status": "pending", "gate": "decision",
            "description": f"acceptance of {tid}. " + "long authority text. " * 400,
            "task_context": {"operator_rulings": {"current": current, "history": []}}}


async def _note(client, tmp_path, monkeypatch, prompt):
    from tests.test_work_admission_3591 import _placeholder

    cid = "dddd1111-1111-2222-3333-444455556666"
    await _placeholder(client, tmp_path, monkeypatch, cid)
    return await ats_context.resolve_prompt_context(
        "http://test", client, {"session_id": cid, "cwd": str(tmp_path), "prompt": prompt})


@pytest.mark.asyncio
async def test_a_large_brief_keeps_rulings_and_every_named_ticket_inside_the_budget(
        client, tmp_path, monkeypatch):
    ids = list(range(4601, 4609))
    envs = {t: _envelope(t, ruling="do not merge the conflicting branch" if t == 4608 else None)
            for t in ids}
    monkeypatch.setattr(briefs, "fetch_tower_task_data", lambda tid, **kw: (envs[int(tid)], None))
    monkeypatch.setattr(briefs, "fetch_task_overlaps", lambda objective, **kw: ([], "not requested"))
    note = await _note(client, tmp_path, monkeypatch,
                       "reconcile ticket " + " ".join(f"#{t}" for t in ids))

    assert len(note) <= 7300
    head = note[:2000]  # what Claude shows when a hook's output overflows
    assert "[PROHIBITION #4608] do not merge the conflicting branch" in head
    for t in ids:  # no named ticket is silently omitted, and all sit in the head
        assert f"#{t} [pending, gate=decision] title of {t}" in head


@pytest.mark.asyncio
async def test_a_huge_exact_ticket_brief_is_cut_with_a_pointer_not_silently(
        client, tmp_path, monkeypatch):
    env = _envelope(4700, ruling="#2945 controls; do not merge the solo-pose branch")
    env["description"] = "acceptance criteria. " + "authority text. " * 3000  # ~50 KB
    monkeypatch.setattr(briefs, "fetch_tower_task_data", lambda tid, **kw: (env, None))
    note = await _note(client, tmp_path, monkeypatch, "continue ticket #4700")

    assert len(note) <= 7300  # stays under Claude's ~10 KB inline limit with the supplement
    assert "[PROHIBITION #4700] #2945 controls" in note[:2000]
    assert "brief truncated here" in note and "task_brief" in note


@pytest.mark.asyncio
async def test_an_unavailable_lookup_is_said_in_the_digest(client, tmp_path, monkeypatch,
                                                          governed):
    monkeypatch.setattr(briefs, "fetch_task_overlaps", lambda objective, **kw: (
        [], "Echo Brain overlap lookup unavailable (ConnectError)"))
    from tests.test_work_admission_3591 import _placeholder

    cid = "eeee1111-1111-2222-3333-444455556666"
    await _placeholder(client, tmp_path, monkeypatch, cid)
    note = await ats_context.resolve_prompt_context(
        "http://test", client, {"session_id": cid, "cwd": governed,
                                "prompt": "tidy the tokenizer comment handling"})
    assert "existing-work lookup UNAVAILABLE" in note
    assert "NOT 'no related work'" in note


def test_a_small_brief_is_not_truncated():
    assert ats_context._within_budget("TASK BRIEF — x\nshort", 5000) == "TASK BRIEF — x\nshort"


def test_digest_order_is_rulings_named_branches_candidates_more():
    packet = {
        "task_id": None, "task_context": {}, "task_envelope": {},
        "named_tasks": [{"id": 1, "title": "one", "status": "pending", "gate": "none",
                         "operator_rulings": [{"prohibition": True, "ruling": "R1"}],
                         "live_owner": {"agent": "codex:x", "session_id": "s"}}],
        "active_branches": [{"branch": "fix/1-a", "tickets": [1], "unique_commits": 2,
                             "conflict": "", "files_in_scope": [], "ticket_rulings": {}}],
        "overlaps": [{"id": 9, "title": "nine", "status": "pending", "operator_rulings": []}],
        "overlaps_status": "ok (1 candidate(s))",
    }
    text = "\n".join(ats_context._priority_digest(packet))
    order = [text.index(k) for k in ("[PROHIBITION #1] R1", "#1 [pending, gate=none] one",
                                    "live owner: codex:x", "fix/1-a (#1)", "#9 [pending]", "more:")]
    assert order == sorted(order)


def test_one_ruling_seen_through_the_ticket_and_its_branch_is_listed_once():
    ruling = {"prohibition": True, "ruling": "#2945 remains authoritative."}
    packet = {"task_id": 3528, "task_envelope": {"id": 3528},
              "task_context": {"operator_rulings": {"current": [ruling]}},
              "named_tasks": [], "overlaps": [], "overlaps_status": "not requested",
              "active_branches": [{"branch": "fix/3528-x", "tickets": [3528], "unique_commits": 1,
                                   "files_in_scope": [], "ticket_rulings": {"3528": [ruling]}}]}
    text = "\n".join(ats_context._priority_digest(packet))
    assert text.count("[PROHIBITION #3528] #2945 remains authoritative.") == 1
