"""The context packet handed to a worker when it claims work.

The point is LESS but BETTER context: cheap local tokens spent so the expensive
model starts with the useful part. Two rules the tests exist to hold:

  * every line is attributable — a brief with an unciteable claim is a very
    efficient way to remember a hallucination forever;
  * recall is best-effort — Echo Brain or ollama being down degrades the brief,
    never the claim.
"""

from __future__ import annotations

import pytest

from ai_team_sync.briefs import (OBSERVATION, OPERATOR_DECISION, VERIFIED,
                                 INFERRED, BriefItem, classify_memory,
                                 compress, rerank_by_similarity)


def test_an_operator_trusted_memory_outranks_a_model_written_one():
    op = classify_memory({"payload": {"trust": "operator_memory"}, "content": "x"})
    model = classify_memory({"payload": {"trust": "inferred"}, "content": "x"})

    assert op == OPERATOR_DECISION
    assert model == INFERRED


def test_a_memory_with_no_trust_marker_is_never_promoted():
    assert classify_memory({"content": "root cause was X"}) == INFERRED


def test_compression_dedups_and_budgets_without_inventing_text():
    items = [
        BriefItem(OBSERVATION, "lock on src/** held by codex", "ats:lock/1"),
        BriefItem(OBSERVATION, "lock on src/** held by codex", "ats:lock/1"),
        BriefItem(INFERRED, "a" * 900, "echo:mem/2"),
    ]

    out = compress(items, max_items=5, max_chars=200)

    assert len(out) == 2, "identical claim from the same citation collapses"
    assert len(out[1].text) <= 203, "long items are cut, never summarized away"
    assert out[1].text.endswith("...")
    assert out[1].citation == "echo:mem/2"


def test_compression_keeps_the_highest_authority_when_it_must_drop():
    items = [BriefItem(INFERRED, f"guess {i}", f"echo:mem/{i}", score=0.9) for i in range(5)]
    items.append(BriefItem(OPERATOR_DECISION, "do not rerun the known-broken lane",
                           "echo:mem/rule", score=0.1))

    out = compress(items, max_items=2, max_chars=500)

    assert out[0].provenance == OPERATOR_DECISION, "a ruling outranks a high-scoring guess"


def test_rerank_falls_back_to_the_original_order_when_local_embedding_is_down(monkeypatch):
    import ai_team_sync.briefs as briefs

    def boom(*a, **kw):
        raise RuntimeError("ollama down")

    monkeypatch.setattr(briefs, "_embed", boom)
    items = [BriefItem(INFERRED, "first", "echo:mem/1"),
             BriefItem(INFERRED, "second", "echo:mem/2")]

    out = rerank_by_similarity("anything", items)

    assert [i.text for i in out] == ["first", "second"], "recall degrades, it never raises"


def test_rerank_orders_by_similarity_to_the_objective(monkeypatch):
    import ai_team_sync.briefs as briefs

    vectors = {
        "identity binding for two characters": [1.0, 0.0],
        "two-character identity resolver": [0.9, 0.1],
        "apple pie recipe": [0.0, 1.0],
    }
    monkeypatch.setattr(briefs, "_embed", lambda texts: [vectors[t] for t in texts])
    items = [BriefItem(INFERRED, "apple pie recipe", "echo:mem/1"),
             BriefItem(INFERRED, "two-character identity resolver", "echo:mem/2")]

    out = rerank_by_similarity("identity binding for two characters", items)

    assert out[0].text == "two-character identity resolver"


@pytest.mark.asyncio
async def test_brief_on_claim_carries_live_ats_state_and_cites_it(client, monkeypatch):
    import ai_team_sync.briefs as briefs
    monkeypatch.setattr(briefs, "recall_memories", lambda *a, **kw: [])

    holder = await client.post("/api/sessions", json={
        "developer": "patrick", "agent": "codex", "scope": ["src/render/**"],
        "description": "bounded render repair", "repo_root": "/opt/anime-studio",
        "auto_lock": True,
    })
    assert holder.status_code == 201
    sid = holder.json()["id"]
    await client.post(f"/api/sessions/{sid}/decisions", json={
        "title": "Do not rerun the pair lane",
        "chosen": "park the cohort",
        "reasoning": "checkpoint-invariant rejection; the checkpoint is not the lever",
    })

    brief = await client.post("/api/brief", json={
        "objective": "fix the two-body render lane",
        "repo_root": "/opt/anime-studio",
        "scope": ["src/render/**"],
        "recall": False,
    })

    assert brief.status_code == 200
    body = brief.json()
    blockers = " ".join(i["text"] + i["citation"] for i in body["blockers"])
    decisions = " ".join(i["text"] + i["citation"] for i in body["decisions"])
    assert "src/render/**" in blockers and "codex" in blockers
    assert "Do not rerun the pair lane" in decisions
    assert "ats:" in decisions, "every line is attributable"
    assert body["objective"] == "fix the two-body render lane"


@pytest.mark.asyncio
async def test_a_brief_never_fails_the_claim_when_recall_is_down(client, monkeypatch):
    import ai_team_sync.briefs as briefs

    def boom(*a, **kw):
        raise RuntimeError("echo brain down")

    monkeypatch.setattr(briefs, "recall_memories", boom)

    brief = await client.post("/api/brief", json={
        "objective": "anything at all", "repo_root": "/opt/anime-studio", "scope": [],
    })

    assert brief.status_code == 200
    assert brief.json()["recall_status"].startswith("unavailable")


@pytest.mark.asyncio
async def test_decisions_from_another_repo_do_not_leak_into_the_brief(client, monkeypatch):
    import ai_team_sync.briefs as briefs
    monkeypatch.setattr(briefs, "recall_memories", lambda *a, **kw: [])

    other = await client.post("/api/sessions", json={
        "developer": "patrick", "agent": "codex", "scope": ["src/**"],
        "description": "unrelated repo", "repo_root": "/home/patrick/code/ai-team-sync",
    })
    await client.post(f"/api/sessions/{other.json()['id']}/decisions", json={
        "title": "Unrelated choice", "chosen": "something", "reasoning": "elsewhere",
    })

    brief = await client.post("/api/brief", json={
        "objective": "fix the render lane", "repo_root": "/opt/anime-studio",
        "scope": [], "recall": False,
    })

    titles = " ".join(i["text"] for i in brief.json()["decisions"])
    assert "Unrelated choice" not in titles


def test_a_memory_that_states_its_citation_keeps_it():
    """Clerk rows carry the whole provenance chain in the payload.

    Falling through to a content digest threw that away and handed the reader
    'echo:sha1/6bcd622d' instead of the fact id and the session it came from.
    """
    from ai_team_sync.briefs import _memory_citation

    cite = _memory_citation({"payload": {"citation": "project_facts/43 · ats:session/4df033bf",
                                         "file_path": "/tmp/irrelevant.md"},
                             "content": "x"})

    assert cite == "project_facts/43 · ats:session/4df033bf"


# --- the preflight hint ----------------------------------------------------

def test_the_hint_fires_on_an_operator_ruling_in_scope():
    from ai_team_sync.briefs import OPERATOR_DECISION, BriefItem, preflight_hint

    on, why = preflight_hint([], [BriefItem(OPERATOR_DECISION, "do not rerun the lane",
                                            "echo:/…/operator_rule.md")])

    assert on and "operator_rule.md" in why


def test_the_hint_fires_on_a_recorded_prohibition():
    from ai_team_sync.briefs import INFERRED, BriefItem, preflight_hint

    on, why = preflight_hint(
        [BriefItem(INFERRED, "Park the cohort: do not rerun the pair lane",
                   "ats:decision/abc")], [])

    assert on and "ats:decision/abc" in why


def test_the_hint_stays_quiet_for_ordinary_history():
    from ai_team_sync.briefs import INFERRED, BriefItem, preflight_hint

    on, why = preflight_hint(
        [BriefItem(INFERRED, "Route the render through the guarded lane",
                   "ats:decision/z")], [])

    assert on is False and why is None


def test_a_false_hint_is_not_a_clear_verdict():
    """False means the cheap trigger found nothing, not that preflight would pass."""
    from ai_team_sync.briefs import preflight_hint

    on, why = preflight_hint([], [])

    assert on is False and why is None


@pytest.mark.asyncio
async def test_the_brief_carries_the_hint_fields(client, monkeypatch):
    import ai_team_sync.briefs as briefs
    monkeypatch.setattr(briefs, "recall_memories", lambda *a, **kw: [])

    resp = await client.post("/api/brief", json={
        "objective": "anything", "repo_root": "/opt/anime-studio",
        "scope": [], "recall": False})

    body = resp.json()
    assert "preflight_recommended" in body and body["preflight_recommended"] is False
    assert "preflight_reason" in body
