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

    def should_auto_approve(self, request: OverrideRequest) -> bool | None:
        """
        Evaluate if request should be auto-approved.

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

        # No automatic decision
        return None

    def get_auto_response_message(self, approved: bool) -> str:
        """Get automatic response message."""
        if approved:
            return "Auto-approved based on justification keywords"
        else:
            return "Auto-denied based on policy rules"
