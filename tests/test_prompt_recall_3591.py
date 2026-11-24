"""#3591: prior work recalled for a prompt reaches the agent's context.

The chained Echo prompt hook writes recall to the status line only (operator
2026-06-07), so the ATS hook asks its brief for recall itself and lists the top
items, as text with their citations, in the priority digest that is never cut.
"""
from __future__ import annotations

import asyncio

from ai_team_sync.context_resolution import RequestTarget
from ai_team_sync.hooks import ats_context

STOP = ("[user]: STOP before implementing another geometry generator. You found that "
        "Echo already has generate_scale_disparity_ref. Do not duplicate it.")


def _packet(recall):
    return {"task_id": None, "task_context": {}, "task_envelope": {}, "named_tasks": [],
            "active_branches": [], "overlaps_status": "ok (1 candidate(s))",
            "overlaps": [{"id": 9, "title": "nine", "status": "pending"}], "recall": recall}


def test_recalled_prior_work_is_in_the_digest_as_text_with_its_citation():
    text = "\n".join(ats_context._priority_digest(_packet([
        {"provenance": "SEMANTIC_MEMORY", "text": STOP, "citation": "claude-code:a0679e5c.jsonl"},
        {"provenance": "SEMANTIC_MEMORY", "text": "second", "citation": "echo:sha1/2"},
        {"provenance": "SEMANTIC_MEMORY", "text": "third", "citation": "echo:sha1/3"},
        {"provenance": "SEMANTIC_MEMORY", "text": "fourth", "citation": "echo:sha1/4"}])))
    assert "Echo already has generate_scale_disparity_ref" in text
    assert "↳ claude-code:a0679e5c.jsonl" in text
    assert "third" in text and "fourth" not in text
    assert "not authority" in text
    assert text.index("#9 [pending]") < text.index("generate_scale_disparity_ref") \
        < text.index("retrieve (verified tools")


def test_no_recall_adds_no_section():
    text = "\n".join(ats_context._priority_digest(_packet([])))
    assert "prior work recalled" not in text


def test_the_hook_asks_its_brief_for_recall():
    sent = {}

    class Resp:
        status_code = 200

        def json(self):
            return {"rendered": "TASK BRIEF — do x"}

    class Client:
        async def post(self, url, json=None, headers=None, timeout=None):
            sent.update(json)
            return Resp()

    asyncio.run(ats_context._brief(Client(), "http://ats", "s", "", "do x",
                                   RequestTarget(None, "/r", "governed_cwd")))
    assert sent["recall"] is True
