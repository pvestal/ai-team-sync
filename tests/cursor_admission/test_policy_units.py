"""Construction/validation tests only; passing these proves no Cursor behavior."""

import pytest
from .contract import CHECKS, POLICY_VERSION, ReadOnlyPolicy, admission_yes, hook_allows, validate_provenance, validate_result


@pytest.fixture
def policy(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "source.py").write_text("value = 1\n")
    outside = tmp_path / "secret"
    outside.write_text("outside")
    (repo / "escape").symlink_to(outside)
    (repo / ".env").write_text("secret")
    return ReadOnlyPolicy(repo, 2738, "exact-child", "exact-delegation")


def test_bounded_read_surface(policy):
    assert policy.authority == {"edit": "none", "commit": False, "task_close": "no"}
    assert policy.allows("repo.read", {"path": "source.py"})
    for path in ("../secret", "escape", ".env", "missing"):
        assert not policy.allows("repo.read", {"path": path})
    assert policy.allows("tower.get_tower_task", {"task_id": 2738})
    assert not policy.allows("tower.get_tower_task", {"task_id": 2652})
    assert policy.allows("ats.my_authority", {"session_id": "exact-child"})
    assert not policy.allows("ats.my_authority", {"session_id": "parent"})
    assert not policy.allows("ats.my_authority", {"worker": "codex"})
    assert policy.allows("ats.delegation_status", {"delegation_id": "exact-delegation"})
    assert not policy.allows("ats.delegation_status", {"delegation_id": "other"})


@pytest.mark.parametrize("tool", ["repo.write", "shell", "git.commit", "git.push", "network",
    "tower.update_tower_task", "tower.review_gate", "ats.start_session", "ats.extend_scope",
    "ats.log_decision", "ats.complete_session", "ats.delegate", "ats.reconcile_delegation",
    "subagent", "cloud_handoff", "unknown.new_tool"])
def test_unknown_and_mutating_tools_deny(policy, tool):
    assert not policy.allows(tool, {})


@pytest.mark.parametrize("mode", ["VERIFY", "IMPLEMENT", "ask", "unknown"])
def test_unsupported_modes_refuse(tmp_path, mode):
    with pytest.raises(ValueError):
        ReadOnlyPolicy(tmp_path, 2738, "child", "delegation", mode)


@pytest.mark.parametrize("exit_code,timed_out,response", [
    (1, False, {"permission": "allow"}), (0, True, {"permission": "allow"}),
    (0, False, None), (0, False, {}), (0, False, {"permission": "ask"}),
    (0, False, {"permission": "deny"}), (0, False, "invalid JSON"),
])
def test_proposed_hook_supervisor_is_fail_closed(exit_code, timed_out, response):
    assert not hook_allows(exit_code=exit_code, timed_out=timed_out, response=response)


def test_proposed_hook_explicit_allow():
    assert hook_allows(exit_code=0, timed_out=False, response={"permission": "allow"})


def test_all_22_controls_require_executed_evidence():
    assert len(CHECKS) == 22
    good = {k: {"status": "PASS", "evidence": "synthetic validator unit fixture"} for k in CHECKS}
    assert admission_yes(good)
    for key in CHECKS:
        for status in ("NOT_RUN", "SKIP", "FAIL"):
            assert not admission_yes({**good, key: {"status": status, "evidence": "fixture"}})
        assert not admission_yes({**good, key: {"status": "PASS", "evidence": ""}})
    assert not admission_yes({})


@pytest.fixture
def provenance():
    return dict(requested_worker="cursor", worker_harness="cursor", resolved_executable="/fixture/agent",
        realpath="/fixture/pinned/agent", binary_version="UNREAL unit fixture", binary_sha256="a" * 64,
        launch_spec_version="draft", policy_version=POLICY_VERSION, containment_launcher="/fixture/contain",
        argv=["/fixture/contain", "/fixture/agent", "--print", "packet"], workspace="/fixture/repo",
        child_session_id="child", delegation_id="delegation", underlying_model_requested="gpt-example",
        underlying_model_reported=None)


def test_model_never_changes_harness_identity(provenance):
    validate_provenance(provenance, {"child_session_id": "child"})
    with pytest.raises(ValueError):
        validate_provenance({**provenance, "worker_harness": "codex"}, {})


@pytest.mark.parametrize("key", ["binary_version", "binary_sha256", "realpath", "policy_version", "argv", "child_session_id"])
def test_missing_or_mismatched_provenance_refuses(provenance, key):
    bad = dict(provenance)
    bad.pop(key)
    with pytest.raises(ValueError):
        validate_provenance(bad, {})
    with pytest.raises(ValueError):
        validate_provenance(provenance, {key: "mismatch"})


def test_incomplete_wrong_identity_and_history_result_refuse():
    good = dict(child_session_id="child", task_id=2738, packet_sha256="b" * 64, complete=True,
        findings=[dict(file="source.py", line=1, finding="unit finding")],
        constraints=["no writes"], rejected_approaches=["no cloud handoff"])
    kw = dict(child_id="child", task_id=2738, packet_sha256="b" * 64)
    validate_result(good, **kw)
    for key, value in (("complete", False), ("child_session_id", "parent"), ("task_id", 2652),
        ("packet_sha256", "wrong"), ("findings", []), ("constraints", []), ("rejected_approaches", [])):
        with pytest.raises(ValueError):
            validate_result({**good, key: value}, **kw)
