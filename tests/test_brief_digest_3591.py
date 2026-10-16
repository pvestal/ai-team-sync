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
                                    "live owner: codex:x", "fix/1-a (#1)", "#9 [pending]",
                                    "retrieve (verified tools")]
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


# ── Codex confirmation blockers (review e99660fb) ───────────────────────────
#
# 6: the note told agents to "call task_brief (same request)", but task_brief
#    takes ONE task_id and no overlap flag, so a multi-ticket brief cannot be
#    reproduced that way. Only retrieval paths that work may be advertised.
# 7: rulings and candidates past the digest caps were deferred to "the brief
#    below", which can itself be truncated -- a ruling on an unnamed ticket
#    then had no identifier and no working path.

def _ruling(rid: str, text: str, prohibition: bool = True) -> dict:
    return {"id": rid, "effect": "BLOCK" if prohibition else "ADVISORY",
            "prohibition": prohibition, "ruling": text}


def _env_with(tid: int, rulings: list[dict], *, long: bool = False) -> dict:
    return {"id": tid, "title": f"title of {tid}", "status": "pending", "gate": "verify",
            "description": f"acceptance of {tid}. " + ("authority text. " * (3000 if long else 5)),
            "task_context": {"operator_rulings": {"current": rulings, "history": []}}}


def _discoverable(note: str, ticket: int, ruling: dict) -> bool:
    """A ruling is accounted for when its full text is shown, or its ticket and
    stable id are listed together with a working retrieval instruction."""
    if f"#{ticket}] {ruling['ruling']}" in note:
        return True
    line = next((ln for ln in note.splitlines() if ruling["id"][:8] in ln), "")
    return f"#{ticket}" in line and f"task_brief(objective=..., task_id={ticket})" in line


MULTI = (3536, 3560, 3517)


@pytest.mark.asyncio
async def test_codex_blocker_6_the_multi_ticket_note_advertises_only_paths_that_work(
        client, tmp_path, monkeypatch):
    envs = {t: _env_with(t, [_ruling(f"{t}aaaa-0000", f"ruling on {t}")]) for t in MULTI}
    monkeypatch.setattr(briefs, "fetch_tower_task_data", lambda tid, **kw: (envs[int(tid)], None))
    note = await _note(client, tmp_path, monkeypatch,
                       "Reconcile ticket #3536 with #3560 and #3517 before landing")

    assert "same request" not in note  # the broken pointer is gone
    assert "takes ONE task_id" in note  # and the limit is stated where the tool is named
    for t in MULTI:  # every named ticket, with gate and its retrieval paths
        assert f"#{t} [pending, gate=verify] title of {t}" in note
    assert "get_tower_task(task_id=N)" in note
    assert "task_brief(objective=..., task_id=N)" in note

    # Each advertised ATS instruction is executed through the real MCP handler:
    # task_brief with one task_id returns that ticket's current rulings.
    from tests.test_work_admission_3591 import _bridge_mcp
    mcp = _bridge_mcp(client, monkeypatch, tmp_path)
    monkeypatch.setattr(briefs, "fetch_tower_task_data", lambda tid, **kw: (envs[int(tid)], None))
    schema = next(t for t in await mcp.list_tools() if t.name == "task_brief").inputSchema
    assert schema["properties"]["task_id"]["type"] == "integer"
    assert "task_ids" not in schema["properties"]  # why the old pointer could not work
    for t in MULTI:
        text = (await mcp.call_tool("task_brief", {"objective": "check rulings", "task_id": t,
                                                   "recall": False}))[0].text
        assert f"ruling on {t}" in text and f"{t}aaaa-0000" in text


@pytest.mark.asyncio
async def test_codex_blocker_7_a_ruling_past_every_cap_stays_identified_and_retrievable(
        client, tmp_path, monkeypatch):
    # One named ticket with two rulings, six overlap candidates (the lookup's
    # maximum) each carrying a ruling: eight rulings, more than five related
    # tickets. The LAST candidate's prohibition is the important one.
    named = _env_with(4800, [_ruling("n1-000000", "named ruling one"),
                             _ruling("n2-000000", "named ruling two", prohibition=False)],
                      long=True)  # ~50 KB: the full brief is truncated
    cands = [{"id": 4810 + i, "title": f"candidate {i}", "status": "pending",
              "operator_rulings": [_ruling(f"c{i}-000000", f"candidate ruling {i}",
                                           prohibition=(i == 5))]}
             for i in range(6)]
    important = cands[-1]["operator_rulings"][0]
    monkeypatch.setattr(briefs, "fetch_tower_task_data", lambda tid, **kw: (named, None))
    monkeypatch.setattr(briefs, "fetch_task_overlaps", lambda objective, **kw: (cands, None))
    note = await _note(client, tmp_path, monkeypatch,
                       "continue #4800 and the related solo pose work across the candidates")

    assert len(note) <= 7300 and "brief truncated here" in note
    assert "same request" not in note
    assert _discoverable(note, 4800, named["task_context"]["operator_rulings"]["current"][0])
    assert _discoverable(note, 4800, named["task_context"]["operator_rulings"]["current"][1])
    for c in cands:  # every candidate identified, every ruling accounted for
        assert f"#{c['id']}" in note
        assert _discoverable(note, c["id"], c["operator_rulings"][0])
    # prohibitions lead, so the important one is in the head, in full
    assert f"[PROHIBITION #4815] {important['ruling']}" in note[:2000]
    assert "CONTEXT INCOMPLETE —" not in note  # everything fit: no incompleteness marker


def test_rulings_past_the_detail_cap_are_listed_by_ticket_and_id():
    packet = {"task_id": None, "task_context": {}, "task_envelope": {}, "active_branches": [],
              "overlaps_status": "not requested", "overlaps": [],
              "named_tasks": [{"id": 5000 + i, "title": f"t{i}", "status": "pending",
                               "gate": "none", "operator_rulings": [
                                   _ruling(f"r{i:02d}-111111", f"ruling text {i}")]}
                              for i in range(9)]}
    text = "\n".join(ats_context._priority_digest(packet))
    for i in range(9):
        r = packet["named_tasks"][i]["operator_rulings"][0]
        assert _discoverable(text, 5000 + i, r), i
    assert "NOT SHOWN IN FULL ABOVE — 3 item(s)" in text


def test_branch_tickets_the_server_never_checked_for_rulings_are_named_not_hidden():
    from ai_team_sync.briefs import _BRANCH_TICKET_RULINGS_CAP as cap

    branches = [{"branch": f"fix/{6000 + i}-x", "tickets": [6000 + i], "unique_commits": 1,
                 "conflict": "merges_cleanly", "files_in_scope": ["a.py"], "ticket_rulings": {}}
                for i in range(cap + 2)]
    packet = {"task_id": None, "task_context": {}, "task_envelope": {}, "named_tasks": [],
              "overlaps": [], "overlaps_status": "not requested", "active_branches": branches,
              "active_branches_status": f"ok ({len(branches)} branch(es))"}
    text = "\n".join(ats_context._priority_digest(packet, "/repo"))
    unchecked = [6000 + cap, 6000 + cap + 1]
    line = next(ln for ln in text.splitlines() if "NOT CHECKED" in ln)
    assert all(f"#{t}" in line for t in unchecked)
    assert "task_brief(objective=..., task_id=N)" in line
    assert all(f"#{6000 + i}" not in line for i in range(cap))
    for b in branches[3:]:  # branches past the detail cap: named, with a git path
        assert f"{b['branch']} " in text
    assert "git -C /repo log --oneline -10 <branch>" in text


def test_a_failed_branch_census_is_unavailable_never_none():
    packet = {"task_id": None, "task_context": {}, "task_envelope": {}, "named_tasks": [],
              "overlaps": [], "overlaps_status": "not requested", "active_branches": [],
              "active_branches_status": "unavailable (TimeoutExpired)"}
    text = "\n".join(ats_context._priority_digest(packet, "/repo"))
    assert "unfinished-branch census UNAVAILABLE" in text
    assert "NOT 'no unfinished branches'" in text


def test_an_index_too_large_for_the_budget_says_incomplete_with_working_paths():
    named = [{"id": 7000 + i, "title": f"ticket {i}", "status": "pending", "gate": "none",
              "operator_rulings": [_ruling(f"x{i:03d}-000000", f"ruling {i} " + "z" * 150)]}
             for i in range(150)]
    cands = [{"id": 7900 + i, "title": f"cand {i}", "status": "pending",
              "operator_rulings": [_ruling(f"k{i}-000000", f"cand ruling {i}")]}
             for i in range(6)]
    packet = {"task_id": None, "task_context": {}, "task_envelope": {}, "named_tasks": named,
              "overlaps": cands, "overlaps_status": "ok (6 candidate(s))",
              "active_branches": [], "active_branches_status": "ok (0 branch(es))"}
    lines = ats_context._priority_digest(packet, "/repo")
    text = "\n".join(lines)
    assert len(text) <= ats_context._DIGEST_BUDGET
    assert "CONTEXT INCOMPLETE" in text
    assert "does NOT establish that every controlling decision is shown" in text
    # what could not be listed is still reachable: the tickets of unlisted
    # rulings that the request did not name are given by id, the rest are the
    # ticket numbers in the request itself
    for c in cands:
        r = c["operator_rulings"][0]
        assert f"#{c['id']}" in text and (r["id"][:8] in text or
                                          "rulings not listed are on" in text)
    assert "every ticket number in your request" in text
    assert "task_brief(objective=..., task_id=N)" in text
    # never claims completeness it cannot establish
    assert "nothing omitted" not in text


# ── possible ticket references (operator ruling "#3591 STAGE 3", item 11) ────
#
# A bare "#3528" counts as a ticket only when the request names it as one
# ("ticket #3528", a leading "#3528", "continue #3528"; briefs.explicit_task_ids).
# "...to finish #3528 (related: #2945)?" resolved with NO tickets, so the
# #3528 prohibition was never fetched. The hook now lists such references as
# advisory POSSIBLE TICKET REFERENCES with working per-ticket retrieval calls
# -- never looked up, never anchored, never a new failure path.

REAL_PHRASING = "Should I merge branch fix/3528-solo-hero-flux-pose to finish #3528 (related: #2945)?"


@pytest.mark.asyncio
async def test_the_real_phrasing_surfaces_both_references_with_working_calls(
        client, tmp_path, monkeypatch, governed):
    from tests.test_work_admission_3591 import _placeholder

    def never(tid, **kw):  # not anchored, not fetched: no new fail-closed path
        raise AssertionError(f"possible reference #{tid} must not be looked up")
    monkeypatch.setattr(briefs, "fetch_tower_task_data", never)
    cid = "ffff1111-1111-2222-3333-444455556666"
    sid = await _placeholder(client, tmp_path, monkeypatch, cid)
    note = await ats_context.resolve_prompt_context(
        "http://test", client, {"session_id": cid, "cwd": governed, "prompt": REAL_PHRASING})

    assert "POSSIBLE TICKET REFERENCES" in note
    for t in (3528, 2945):
        line = next(ln for ln in note.splitlines() if ln.strip().startswith(f"#{t} →"))
        assert f"task_brief(objective=..., task_id={t})" in line
        assert f"get_tower_task(task_id={t})" in line
    assert len(note) <= 7300
    session = (await client.get(f"/api/sessions/{sid}")).json()
    assert session.get("ticket_id") in (None, 0)  # the session was not anchored to them


@pytest.mark.asyncio
async def test_named_tickets_are_not_repeated_as_possible_references(
        client, tmp_path, monkeypatch):
    envs = {t: _env_with(t, []) for t in MULTI}
    monkeypatch.setattr(briefs, "fetch_tower_task_data", lambda tid, **kw: (envs[int(tid)], None))
    note = await _note(client, tmp_path, monkeypatch,
                       "Reconcile ticket #3536 with #3560 and #3517 before landing")
    assert "POSSIBLE TICKET REFERENCES" not in note
    for t in MULTI:  # normal multi-ticket behavior is unchanged
        assert f"#{t} [pending, gate=verify] title of {t}" in note


def test_possible_references_exclude_resolved_tickets_and_short_numbers():
    packet = {"task_id": 4100, "task_context": {}, "task_envelope": {"id": 4100},
              "named_tasks": [{"id": 4101, "title": "x", "status": "pending", "gate": "none"}],
              "overlaps": [], "overlaps_status": "not requested", "active_branches": []}
    text = "\n".join(ats_context._priority_digest(
        packet, "/repo", prompt="ticket #4100, #4101, PR #12, issue #3528 and #3528 again, #99999"))
    refs = [ln.strip().split()[0] for ln in text.splitlines() if ln.strip().startswith("#") and "→" in ln]
    assert refs == ["#3528", "#99999"]


def test_many_possible_references_stay_inside_the_budget_and_say_incomplete():
    packet = {"task_id": None, "task_context": {}, "task_envelope": {}, "named_tasks": [],
              "overlaps": [], "overlaps_status": "not requested", "active_branches": []}
    prompt = " ".join(f"#{1000 + i}" for i in range(300))
    text = "\n".join(ats_context._priority_digest(packet, "/repo", prompt=prompt))
    assert len(text) <= ats_context._DIGEST_BUDGET
    assert "CONTEXT INCOMPLETE" in text
    assert "possible ticket references not listed: every #N in your request" in text
