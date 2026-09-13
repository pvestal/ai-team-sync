"""Worker identity, capability and authority — ATS as a worker-neutral control plane.

The lock guard that protects a claimed scope runs as a Claude Code PreToolUse
hook. Codex has no hook mechanism and a local model has no client at all, so
anything enforced client-side is advice for everyone except Claude. Authority
therefore lives HERE, on the server, where every worker meets it equally.
"""

import pytest

from ai_team_sync.workers import Worker, registry


def test_a_known_worker_resolves_by_its_session_label():
    # Sessions carry 'claude-code:<cid8>'; the registry is keyed by worker.
    w = registry().resolve("claude-code:fb0bb6bf")

    assert w.name == "claude-code"
    assert w.authority.edit == "claimed_scope"
    assert w.may_claim_scope


def test_a_local_model_resolves_through_its_family_prefix():
    w = registry().resolve("local:qwen3-30b")

    assert w.name == "local"
    assert w.cost_class == "local"
    assert not w.may_claim_scope, "a read-only worker must not claim an edit scope"
    assert not w.may_commit
    assert not w.may_close_task


def test_an_unregistered_worker_fails_closed():
    w = registry().resolve("some-new-thing-nobody-registered")
    assert w.name == "restricted"
    assert w.authority.edit == "none"
    assert w.authority.commit is False
    assert w.authority.task_close == "no"


def test_strict_mode_drops_an_unregistered_worker_to_read_only(monkeypatch):
    """The obsolete strict flag cannot change the fail-closed invariant."""
    monkeypatch.setenv("ATS_STRICT_WORKERS", "1")
    registry.cache_clear()

    w = registry().resolve("some-new-thing-nobody-registered")

    assert w.name == "restricted"
    assert not w.may_claim_scope
    assert w.capabilities == ("repo_read",)
    registry.cache_clear()


def test_strict_mode_never_touches_a_registered_worker(monkeypatch):
    monkeypatch.setenv("ATS_STRICT_WORKERS", "1")
    registry.cache_clear()

    assert registry().resolve("claude-code:fb0bb6bf").may_claim_scope
    assert not registry().resolve("local:qwen3-30b").may_claim_scope
    registry.cache_clear()


def test_codex_closes_on_the_same_conditional_terms_as_claude():
    """Operator ruling 2026-09-12: frontier close authority is model-neutral.

    Note what `may_close_task` can and cannot tell you. It is the UNCONDITIONAL
    question, so it reads False for 'no' and for 'conditional' alike — which is
    why the class is asserted by value here. See
    tests/test_codex_close_authority.py for the full tranche.
    """
    w = registry().resolve("codex")

    assert w.may_claim_scope
    assert w.may_commit
    assert w.authority.task_close == "conditional"
    assert w.authority.task_close == registry().resolve("claude-code").authority.task_close
    assert not w.may_close_task, "conditional is not unconditional; the evidence decides"


def test_capability_questions_are_answered_not_guessed():
    w = registry().resolve("local:qwen3-30b")

    assert w.can("log_triage")
    assert not w.can("multi_file_edit")


def test_a_registry_file_overrides_and_extends_the_builtins(tmp_path, monkeypatch):
    cfg = tmp_path / "workers.toml"
    cfg.write_text(
        "[workers.local]\n"
        "capabilities = ['repo_read', 'failure_cluster']\n"
        "cost_class = 'local'\n"
        "concurrency = 3\n"
        "[workers.local.authority]\n"
        "edit = 'none'\n"
        "commit = false\n"
        "task_close = 'no'\n"
        "\n"
        "[workers.'bench-runner']\n"
        "capabilities = ['repo_read', 'benchmark']\n"
        "[workers.'bench-runner'.authority]\n"
        "edit = 'none'\n"
    )
    monkeypatch.setenv("ATS_WORKERS_CONFIG", str(cfg))
    registry.cache_clear()

    local = registry().resolve("local:qwen3-30b")
    bench = registry().resolve("bench-runner")

    assert local.concurrency == 3, "the file wins over the builtin"
    assert bench.can("benchmark"), "a worker the builtins never heard of"
    assert not bench.may_claim_scope
    registry.cache_clear()


def test_a_malformed_registry_file_does_not_take_the_server_down(tmp_path, monkeypatch):
    cfg = tmp_path / "workers.toml"
    cfg.write_text("this is not toml [[[")
    monkeypatch.setenv("ATS_WORKERS_CONFIG", str(cfg))
    registry.cache_clear()

    # Coordination must never wedge real work: fall back to the builtins.
    assert registry().resolve("claude-code").may_claim_scope
    registry.cache_clear()


@pytest.mark.parametrize("label,expected", [
    ("claude-code:fb0bb6bf", "claude-code"),
    ("local:qwen3-30b:7f2a", "local"),
    ("codex", "codex"),
    ("", "restricted"),
])
def test_label_resolution_strips_suffixes_until_it_matches(label, expected):
    assert registry().resolve(label).name == expected


def test_every_builtin_declares_a_complete_authority_block():
    for name in registry().names():
        w = registry().resolve(name)
        assert isinstance(w, Worker)
        assert w.authority.edit in ("none", "claimed_scope")
        assert w.authority.task_close in ("no", "conditional", "yes")
        assert isinstance(w.authority.commit, bool)


def test_the_legacy_unknown_agent_is_restricted():
    """Operator ruling includes legacy clients; compatibility grants no writes."""
    w = registry().resolve("unknown")
    assert w.name == "restricted"
    assert w.authority.edit == "none"
    assert w.authority.commit is False
    assert w.authority.task_close == "no"
