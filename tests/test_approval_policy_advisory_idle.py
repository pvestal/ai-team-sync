"""Advisory-idle auto-approval (ats-git-diff-merge-workflow-p01, targeted fix).

Override requests against ADVISORY locks whose owner session is idle
auto-approve — the operator stops being the relay. Exclusive locks, live
owners, and fresh sessions keep the manual/push path. Keyword policies keep
priority. Idle is the session-activity signal (started_at / heartbeat / lock
creation), so a just-registered session is never auto-overridden.
"""
import pytest

from ai_team_sync.approval_policy import ApprovalPolicy
from ai_team_sync.models import OverrideRequest


def _request(justification="routine file edit"):
    return OverrideRequest(
        requester_session_id="req-1",
        owner_session_id="own-1",
        conflicting_pattern="src/**",
        justification=justification,
    )


def _policy(monkeypatch, approval_config):
    monkeypatch.setattr(
        "ai_team_sync.approval_policy.load_team_config",
        lambda repo_root=None: {"approval": approval_config},
    )
    return ApprovalPolicy()


def test_advisory_idle_owner_auto_approves(monkeypatch):
    policy = _policy(monkeypatch, {})
    assert policy.should_auto_approve(
        _request(), lock_mode="advisory", owner_idle_seconds=700
    ) is True


def test_advisory_live_owner_stays_manual(monkeypatch):
    policy = _policy(monkeypatch, {})
    assert policy.should_auto_approve(
        _request(), lock_mode="advisory", owner_idle_seconds=30
    ) is None


def test_advisory_unknown_idle_stays_manual(monkeypatch):
    # None = liveness unknown -> conservative manual path.
    policy = _policy(monkeypatch, {})
    assert policy.should_auto_approve(
        _request(), lock_mode="advisory", owner_idle_seconds=None
    ) is None


def test_exclusive_lock_never_idle_approves(monkeypatch):
    policy = _policy(monkeypatch, {})
    assert policy.should_auto_approve(
        _request(), lock_mode="exclusive", owner_idle_seconds=10_000
    ) is None


def test_deny_keyword_beats_advisory_idle(monkeypatch):
    policy = _policy(monkeypatch, {"auto_deny_keywords": ["migration"]})
    assert policy.should_auto_approve(
        _request("touching the migration files"),
        lock_mode="advisory", owner_idle_seconds=10_000,
    ) is False


def test_threshold_configurable(monkeypatch):
    policy = _policy(monkeypatch, {"advisory_idle_approve_after_s": 60})
    assert policy.should_auto_approve(
        _request(), lock_mode="advisory", owner_idle_seconds=90
    ) is True


def test_rule_can_be_disabled(monkeypatch):
    policy = _policy(monkeypatch, {"advisory_idle_auto_approve": False})
    assert policy.should_auto_approve(
        _request(), lock_mode="advisory", owner_idle_seconds=10_000
    ) is None


def test_backcompat_no_lock_context_keyword_only(monkeypatch):
    # Callers that don't pass lock context get the original keyword behavior.
    policy = _policy(monkeypatch, {"auto_approve_keywords": ["hotfix"]})
    assert policy.should_auto_approve(_request("urgent hotfix")) is True
    assert policy.should_auto_approve(_request()) is None
