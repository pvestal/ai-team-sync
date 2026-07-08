"""Auto-approval policy evaluation for override requests."""

from __future__ import annotations

from pathlib import Path

from ai_team_sync.config import load_team_config
from ai_team_sync.models import OverrideRequest


class ApprovalPolicy:
    """Evaluates override requests against configured policies."""

    def __init__(self, repo_root: Path | None = None):
        """Load approval policies from .ai-team-sync.toml."""
        config = load_team_config(repo_root)
        approval_config = config.get("approval", {})

        self.auto_approve_keywords = approval_config.get("auto_approve_keywords", [])
        self.auto_deny_keywords = approval_config.get("auto_deny_keywords", [])
        self.timeout_action = approval_config.get("timeout_action", "expire")
        self.llm_evaluate = approval_config.get("llm_evaluate", False)
        # Advisory-idle rule (ats-git-diff-merge-workflow-p01): an override
        # against an ADVISORY lock whose owner session is idle auto-approves,
        # so the operator stops being the relay (3 of the first 6 requests
        # expired unanswered). Exclusive locks always stay manual.
        self.advisory_idle_auto_approve = approval_config.get(
            "advisory_idle_auto_approve", True)
        self.advisory_idle_approve_after_s = approval_config.get(
            "advisory_idle_approve_after_s", 600)

    def should_auto_approve(
        self,
        request: OverrideRequest,
        *,
        lock_mode: str | None = None,
        owner_idle_seconds: float | None = None,
    ) -> bool | None:
        """
        Evaluate if request should be auto-approved.

        Keyword args are optional lock context — omitting them keeps the
        original keyword-only behavior:
            lock_mode: mode of the conflicting lock ("advisory"|"exclusive").
            owner_idle_seconds: seconds since the owner session's newest
                activity (started_at / heartbeat / lock creation — the same
                signal team_status staleness uses). None = unknown -> manual.
                A just-registered session has near-zero idle, so fresh
                sessions are never auto-overridden.

        Returns:
            True - Auto-approve
            False - Auto-deny
            None - Requires manual decision
        """
        justification_lower = request.justification.lower()

        # Check auto-deny keywords first (higher priority)
        for keyword in self.auto_deny_keywords:
            if keyword.lower() in justification_lower:
                return False

        # Check auto-approve keywords
        for keyword in self.auto_approve_keywords:
            if keyword.lower() in justification_lower:
                return True

        # Advisory-idle rule: advisory locks only, and only when enabled.
        if (
            self.advisory_idle_auto_approve
            and lock_mode == "advisory"
            and owner_idle_seconds is not None
            and owner_idle_seconds > self.advisory_idle_approve_after_s
        ):
            return True

        # No automatic decision
        return None

    def get_auto_response_message(self, approved: bool) -> str:
        """Get automatic response message."""
        if approved:
            return ("Auto-approved by policy (approve-keyword match or "
                    "idle advisory-lock owner)")
        else:
            return "Auto-denied based on policy rules"
