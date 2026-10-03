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
                                 INFERRED, SEMANTIC_MEMORY, BriefItem, classify_memory,
                                 compress, rerank_by_similarity)


def test_semantic_payload_trust_never_promotes_a_hit_to_current_operator_authority():
    op = classify_memory({"payload": {"trust": "operator_memory"}, "content": "x"})
    model = classify_memory({"payload": {"trust": "inferred"}, "content": "x"})

    assert op == SEMANTIC_MEMORY
    assert model == SEMANTIC_MEMORY


def test_a_memory_with_no_trust_marker_is_never_promoted():
    assert classify_memory({"content": "root cause was X"}) == SEMANTIC_MEMORY


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
        "description": "unrelated repo", "repo_root": "/srv/other-repo",
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


# --- deterministic task context -------------------------------------------

TASK_CONTEXT = {
    "version": 1,
    "task": {"id": 4101, "key": "task-a", "project_id": 7,
             "project_name": "Anime Studio"},
    "operator_rulings": {
        "current": [{
            "id": "rule-b", "category": "OPERATOR_DECISION",
            "authority": "operator", "effect": "BLOCK", "state": "current",
            "current": True, "ruling": "Do not use the legacy video lane.",
            "prohibition": True, "scope": {"tower_task_id": 4101},
            "author": {"type": "human", "name": "patrick", "authenticated": True},
            "created_at": "2026-10-02T20:00:00+00:00",
            "source": {"id": "operator-turn-2", "citation": "operator://turn/2",
                       "content_sha256": "b" * 64},
            "supersedes": ["rule-a"], "superseded_by": [],
        }],
        "history": [{
            "id": "rule-a", "category": "OPERATOR_DECISION",
            "authority": "operator", "effect": "ALLOW", "state": "superseded",
            "current": False, "ruling": "The legacy video lane may be used.",
            "prohibition": False, "scope": {"tower_task_id": 4101},
            "author": {"type": "human", "name": "patrick", "authenticated": True},
            "created_at": "2026-10-01T20:00:00+00:00",
            "source": {"id": "operator-turn-1", "citation": "operator://turn/1",
                       "content_sha256": "a" * 64},
            "supersedes": [], "superseded_by": ["rule-b"],
        }],
    },
    "prohibitions": ["rule-b"],
    "verified_facts": [{"category": "VERIFIED", "citation": "tower-task/4101#verified_by",
                        "value": {"commit": "abc123"}}],
    "requires_live_verification": ["git", "database", "services"],
}


@pytest.mark.asyncio
async def test_task_brief_keeps_exact_context_structured_and_filters_ticket_decisions(
        db_session, monkeypatch):
    import ai_team_sync.briefs as briefs
    from ai_team_sync.models import Decision, Session
    monkeypatch.setattr(briefs, "fetch_tower_task_context",
                        lambda task_id, **kw: (TASK_CONTEXT, None))
    monkeypatch.setattr(briefs, "recall_memories", lambda *a, **kw: [])

    for ticket, title in ((4101, "Task A worker proposal"),
                          (4102, "Task B conflicting decision")):
        session = Session(developer="patrick", agent="codex", scope="[]",
                          description=title, repo_root="", ticket_id=ticket)
        db_session.add(session)
        await db_session.flush()
        db_session.add(Decision(session_id=session.id, ticket_id=ticket, title=title,
                                chosen="Use the legacy video lane.",
                                reasoning="not authenticated operator provenance"))
    await db_session.commit()

    body = await briefs.build_brief(
        db_session, objective="continue task A", repo_root="", scope=[],
        task_id=4101)
    assert body["task_context"] == TASK_CONTEXT
    assert [d["provenance"] for d in body["decisions"]] == ["WORKER_PROPOSAL"]
    assert body["decisions"][0]["meta"]["ticket_id"] == 4101
    assert body["decisions"][0]["meta"]["authenticated_operator"] is False
    text = body["rendered"]
    assert "Do not use the legacy video lane" in text
    assert "CURRENT OPERATOR RULINGS / PROHIBITIONS" in text
    assert "NON-CURRENT RULING HISTORY" in text
    assert "Task A worker proposal" in text
    assert "Task B conflicting decision" not in text
    assert "requires live verification: git, database, services" in text

    # The same rendered authority and brief are what a delegated Claude/Codex
    # child receives; no parent conversation is inherited.
    from ai_team_sync.briefs import render_task_envelope
    from ai_team_sync.delegation_packet import build_child_packet
    task_envelope = {
        "id": 4101, "task_key": "task-a", "project_id": 7,
        "project_name": "Anime Studio", "parent_id": None,
        "title": "Synthetic task A", "description": "Acceptance: preserve provenance.",
        "status": "pending", "gate": "decision", "priority": 1,
        "recommendation": "", "notes": "", "blocked_by": [],
        "verified_by": {"commit": "abc123"}, "claim": None,
        "is_closed": False, "task_context": TASK_CONTEXT,
    }
    child_brief = briefs.render({**body, "render_task_context": False})
    packet = build_child_packet(
        mode="READ_ONLY",
        delegation={"id": "deleg-a", "parent_task": "4101",
                    "delegating_worker": "codex",
                    "prohibitions": ["file_write", "git_commit"]},
        objective="continue task A", acceptance="report with citations",
        task_envelope_text=render_task_envelope(task_envelope), brief=child_brief)
    assert "Acceptance: preserve provenance" in packet
    assert "Do not use the legacy video lane" in packet
    assert "Task B conflicting decision" not in packet
    assert "operator://turn/2" in packet
    assert packet.count("Do not use the legacy video lane") == 1


@pytest.mark.asyncio
async def test_exact_task_context_survives_semantic_timeout(db_session, monkeypatch):
    import ai_team_sync.briefs as briefs
    monkeypatch.setattr(briefs, "fetch_tower_task_context",
                        lambda task_id, **kw: (TASK_CONTEXT, None))

    def timeout(*args, **kwargs):
        raise TimeoutError("semantic service timed out")

    monkeypatch.setattr(briefs, "recall_memories", timeout)
    body = await briefs.build_brief(
        db_session, objective="continue task A", repo_root="", scope=[],
        task_id=4101)
    assert body["task_context"]["operator_rulings"]["current"][0]["id"] == "rule-b"
    assert body["recall"] == []
    assert body["recall_status"].startswith("unavailable")


@pytest.mark.asyncio
async def test_stale_high_scoring_semantic_hit_cannot_override_current_ruling(
        db_session, monkeypatch):
    import ai_team_sync.briefs as briefs
    monkeypatch.setattr(briefs, "fetch_tower_task_context",
                        lambda task_id, **kw: (TASK_CONTEXT, None))
    monkeypatch.setattr(briefs, "rerank_by_similarity", lambda objective, items: items)
    monkeypatch.setattr(briefs, "recall_memories", lambda *a, **kw: [{
        "content": "STALE ARCHITECTURE: the legacy video lane is required",
        "score": 0.999,
        "payload": {"task_id": 4101, "trust": "operator_memory",
                    "citation": "echo:stale/1"},
    }])

    body = await briefs.build_brief(
        db_session, objective="continue task A", repo_root="", scope=[],
        task_id=4101)
    assert body["recall"][0]["provenance"] == SEMANTIC_MEMORY
    rendered = body["rendered"]
    assert rendered.index("Do not use the legacy video lane") < rendered.index(
        "STALE ARCHITECTURE: the legacy video lane is required")
