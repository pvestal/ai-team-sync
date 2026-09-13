"""Codex is a PEER frontier worker to Claude for closing Tower work.

Operator ruling 2026-09-12. The Codex-led lead-worker canary stopped correctly
before selecting work, because the registry gave Claude task_close=conditional
and Codex task_close=no, so Codex could never prove the lifecycle it was there
to prove. The ruling makes frontier close authority MODEL-NEUTRAL: Codex joins
Claude in the same conditional class, on the same canonical conditions.

What 'conditional' is, precisely, because the distinction is the whole change:
it is not a weaker yes, it is 'the acceptance evidence decides'. It is answered
at the closure gate against the Tower Task envelope, never here and never on the
worker's own say-so. There is deliberately NO second Codex-specific policy —
both frontier workers carry the same one registry value through the same path.

Three things the ruling does NOT do, each pinned below:
  - it grants nobody unconditional authority ('yes' stays unclaimed);
  - it leaves local, default and restricted at task_close=no;
  - it does not reach delegated children. A READ_ONLY or VERIFY child of a
    conditional parent still closes nothing, because delegated authority is an
    intersection and every mode envelope caps task_close at 'no'.

Why this file exists rather than one edited assertion: `Worker.may_close_task`
is the UNCONDITIONAL property, so it reads False for 'no' and for 'conditional'
alike. Every pre-ruling test asserted only that property, which means it could
not tell the two apart and would have stayed green through this change either
way. The authority CLASS has to be asserted by value.
"""

from __future__ import annotations

import pytest

from ai_team_sync.delegation import (IMPLEMENT, MODES, READ_ONLY, VERIFY,
                                     effective_authority)
from ai_team_sync.workers import registry

# The whole declared registry, pinned by value. A snapshot rather than a
# per-worker assertion so that authority drift ANYWHERE in this table fails a
# test, including a worker a later tranche adds without deciding its class.
EXPECTED = {
    "claude-code": ("claimed_scope", True, "conditional"),
    "codex":       ("claimed_scope", True, "conditional"),
    "local":       ("none", False, "no"),
    "default":     ("claimed_scope", True, "no"),
    "restricted":  ("none", False, "no"),
}

# Unconditional close authority is granted to nobody. This allowlist is the
# explicit-design escape hatch the ruling refers to: adding a name here is a
# deliberate act that shows up in review, and leaving it empty is what makes
# 'no worker is unconditional' a checkable claim instead of a hope.
UNCONDITIONAL_BY_DESIGN: frozenset[str] = frozenset()


def test_claude_remains_conditional():
    """Requirement 1. The ruling raises Codex to Claude, it does not move Claude."""
    assert registry().resolve("claude-code").authority.task_close == "conditional"


def test_codex_is_now_conditional():
    """Requirement 2. The one behaviour change in this tranche."""
    assert registry().resolve("codex").authority.task_close == "conditional"


def test_codex_and_claude_are_the_same_close_authority_class():
    """Model-neutral means identical, not merely both non-zero.

    Asserted as equality so a future divergence has to be deliberate: nudging
    either worker alone breaks this, whichever one moves.
    """
    claude = registry().resolve("claude-code").authority
    codex = registry().resolve("codex").authority

    assert codex.task_close == claude.task_close


def test_conditional_is_not_a_licence_to_close_on_its_own_say_so():
    """Conditional must not collapse into unconditional for either worker.

    `may_close_task` is the unconditional question. Both frontier workers answer
    NO to it; the acceptance evidence answers the real one at the gate.
    """
    for name in ("claude-code", "codex"):
        assert not registry().resolve(name).may_close_task, (
            f"{name} conditional authority must not read as unconditional")


@pytest.mark.parametrize("name", sorted(EXPECTED))
def test_the_declared_authority_of_every_worker_class_is_unchanged(name):
    """Requirements 1 and 3, pinned by value across the whole registry."""
    w = registry().resolve(name)
    assert (w.authority.edit, w.authority.commit, w.authority.task_close) == EXPECTED[name]


def test_the_registry_declares_no_worker_this_tranche_did_not_account_for():
    """A worker added later must be given a class here, not inherit one silently."""
    assert set(registry().names()) == set(EXPECTED)


def test_local_workers_remain_shut_out_of_closing_entirely():
    """Requirement 3, as the standing rule it encodes.

    A local model's pass/fail means nothing against the operator's bar. That is
    expressed as authority, not etiquette, and the ruling explicitly leaves it.
    """
    for label in ("local", "local:qwen3-30b", "local:gpt-oss-20b:7f2a"):
        w = registry().resolve(label)
        assert w.name == "local"
        assert w.authority.task_close == "no"
        assert not w.may_close_task


def test_an_unregistered_worker_still_gets_no_close_authority():
    """Requirement 3 for the fallbacks, and the answer for future workers.

    Cursor and anything else unintegrated resolves to 'restricted' and closes
    nothing. Frontier close authority is granted
    per worker, never inherited by arriving.
    """
    for label in ("cursor", "some-worker-nobody-registered", ""):
        assert registry().resolve(label).authority.task_close == "no"


def test_strict_mode_does_not_change_any_close_authority(monkeypatch):
    monkeypatch.setenv("ATS_STRICT_WORKERS", "1")
    registry.cache_clear()
    try:
        assert registry().resolve("codex").authority.task_close == "conditional"
        assert registry().resolve("claude-code").authority.task_close == "conditional"
        assert registry().resolve("cursor").authority.task_close == "no"
        assert registry().resolve("cursor").name == "restricted"
    finally:
        registry.cache_clear()


def test_no_worker_is_unconditional_unless_explicitly_designed_to_be():
    """Requirement 5. Today the allowlist is empty, so the set must be too."""
    unconditional = {w.name for w in registry().all()
                     if w.authority.task_close == "yes" or w.may_close_task}

    assert unconditional == set(UNCONDITIONAL_BY_DESIGN)


@pytest.mark.parametrize("worker", ["claude-code", "codex"])
@pytest.mark.parametrize("mode", [READ_ONLY, VERIFY])
def test_a_read_only_or_verify_child_never_inherits_close_authority(worker, mode):
    """Requirement 4, at the narrowing function.

    Delegated authority is an intersection. Raising Codex's BASE authority must
    not leak into its children, or the ruling would have widened delegation as
    a side effect of widening the registry.
    """
    w = registry().resolve(worker)
    assert w.authority.task_close == "conditional", "precondition: a parent that could close"

    auth = effective_authority(w, mode)

    assert auth.task_close == "no"
    assert auth.edit == "none"
    assert auth.commit is False


@pytest.mark.parametrize("worker", ["claude-code", "codex"])
def test_not_even_implement_lets_a_child_close_the_parents_task(worker):
    """IMPLEMENT keeps the editing it was granted and still closes nothing."""
    auth = effective_authority(registry().resolve(worker), IMPLEMENT)

    assert auth.edit == "claimed_scope"
    assert auth.commit is True
    assert auth.task_close == "no"


@pytest.mark.parametrize("worker", sorted(EXPECTED))
@pytest.mark.parametrize("mode", MODES)
def test_no_delegation_mode_grants_close_authority_to_any_worker(worker, mode):
    """The general form of requirement 4: no (worker, mode) pair closes anything."""
    assert effective_authority(registry().resolve(worker), mode).task_close == "no"


@pytest.mark.asyncio
async def test_the_rest_surface_reports_codex_conditional(client):
    """The registry value is what a worker actually READS over the API.

    A fresh client asks this endpoint, so an in-process assertion alone would
    not prove the answer Codex receives.
    """
    body = (await client.get("/api/workers/codex")).json()

    assert body["worker"] == "codex"
    assert body["authority"]["task_close"] == "conditional"


@pytest.mark.asyncio
async def test_an_instance_suffixed_codex_label_resolves_to_the_same_class(client):
    """Real sessions are labelled 'codex:<cid8>', not 'codex'."""
    body = (await client.get("/api/workers/codex:a71a2f56")).json()

    assert body["worker"] == "codex"
    assert body["authority"]["task_close"] == "conditional"


@pytest.mark.asyncio
async def test_a_codex_session_reports_conditional_base_and_effective(client):
    """What my_authority renders for an undelegated Codex session: both rows."""
    parent = await client.post("/api/sessions", json={
        "developer": "patrick", "agent": "codex:a71a2f56", "scope": ["src/**"],
        "description": "lead-worker canary", "repo_root": "/opt/anime-studio",
        "auto_lock": True,
    })
    assert parent.status_code == 201, parent.text

    a = (await client.get(f"/api/authority/{parent.json()['id']}")).json()

    assert a["worker"] == "codex"
    assert a["base_authority"]["task_close"] == "conditional"
    assert a["effective_authority"]["task_close"] == "conditional"
    assert a["narrowed"] is False


@pytest.mark.asyncio
async def test_a_delegated_codex_child_reports_no_close_authority(client):
    """Requirement 4 end-to-end over the API, with Codex as the CHILD.

    The base row now says conditional, so this is the case where a reader could
    be misled: the effective row must say no, and the record must say it was
    narrowed.
    """
    parent = (await client.post("/api/sessions", json={
        "developer": "patrick", "agent": "claude-code:parent01", "scope": ["src/**"],
        "description": "owns the task", "repo_root": "/opt/anime-studio",
        "auto_lock": True,
    })).json()

    d = (await client.post("/api/delegations", json={
        "parent_session_id": parent["id"], "parent_task": "2654",
        "delegated_worker": "codex", "mode": READ_ONLY,
        "repo_root": "/opt/anime-studio", "scope": ["src/**"],
        "objective": "trace the closure gate",
        "acceptance": "file:line citations, no edits",
        "lease_minutes": 30,
    })).json()

    child = (await client.post("/api/sessions", json={
        "developer": "patrick", "agent": "codex:child001", "scope": [],
        "description": "delegated read-only trace", "repo_root": "/opt/anime-studio",
        "delegation_id": d["id"],
    })).json()

    a = (await client.get(f"/api/authority/{child['id']}")).json()

    assert a["worker"] == "codex"
    assert a["base_authority"]["task_close"] == "conditional"
    assert a["effective_authority"]["task_close"] == "no"
    assert a["narrowed"] is True
    assert "parent_task_close" in a["prohibitions"]
