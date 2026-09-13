"""Pure proposed policy and evidence checks, NOT Cursor integration/enforcement.

No executable discovery, subprocess, configuration writes or registry changes.
An eventual supervisor must implement the containment and produce observations.
"""

from dataclasses import dataclass
from pathlib import Path
import re

POLICY_VERSION = "cursor-read-only-draft-1"
ISOLATION_KEYS = ("ATS_SESSION_ID", "ATS_STATE_DIR", "ATS_AGENT", "ATS_DELEGATION")
CHECKS = (
    "executable_provenance", "version_hash", "packet_before_objective",
    "exact_child_identity", "mcp_env_isolation", "repository_read",
    "repository_write_denied", "shell_mutation_denied", "lead_sentinel_unchanged",
    "home_write_denied", "commit_push_denied", "direct_network_write_denied",
    "tower_mutation_denied", "ats_mutation_denied", "onward_spawn_denied",
    "hook_crash_denied", "hook_timeout_denied", "invalid_result_rejected",
    "exact_child_finalized", "parent_unchanged", "no_orphans", "unsupported_modes_refused",
)
ATS_READS = frozenset(("my_authority", "get_session_details", "team_status",
    "get_decision_history", "delegation_status", "ats_version"))


@dataclass(frozen=True)
class ReadOnlyPolicy:
    repo: Path
    task_id: int
    child_id: str
    delegation_id: str
    mode: str = "READ_ONLY"

    def __post_init__(self):
        if self.mode != "READ_ONLY" or not self.repo.is_absolute() or self.task_id <= 0:
            raise ValueError("unsupported mode or invalid workspace/task")
        if not self.child_id or not self.delegation_id:
            raise ValueError("exact child and delegation identities required")

    @property
    def authority(self):
        return {"edit": "none", "commit": False, "task_close": "no"}

    def allows(self, tool: str, args: dict) -> bool:
        """Proposed broker policy, not vendor tool-name/config assumptions.

        Normalized broker tool names are exact and unknown names/arguments deny.
        The OS must independently mount the repository read-only.
        """
        if not isinstance(args, dict):
            return False
        if tool == "repo.read" and set(args) == {"path"}:
            try:
                path = (self.repo / args["path"]).resolve(strict=True)
                root = self.repo.resolve(strict=True)
                return path.is_relative_to(root) and path.is_file() and not any(
                    p in (".git", ".cursor") or p.startswith(".env") for p in path.relative_to(root).parts)
            except (OSError, TypeError, ValueError):
                return False
        if tool == "tower.get_tower_task":
            return args == {"task_id": self.task_id}
        if tool == "ats.my_authority" or tool == "ats.get_session_details":
            return args == {"session_id": self.child_id}
        if tool == "ats.delegation_status":
            return args == {"delegation_id": self.delegation_id}
        if tool.startswith("ats.") and tool[4:] in ATS_READS:
            return args == {}
        if tool == "report.return":
            return set(args) == {"child_session_id", "result"} and args["child_session_id"] == self.child_id
        return False


def hook_allows(*, exit_code, timed_out, response) -> bool:
    """Proposed supervisor rule; crash/timeout/invalid output deny regardless of Cursor."""
    return exit_code == 0 and timed_out is False and isinstance(response, dict) and response == {"permission": "allow"}


def validate_provenance(observation: dict, expected: dict) -> None:
    required = ("requested_worker", "worker_harness", "resolved_executable", "realpath",
        "binary_version", "binary_sha256", "launch_spec_version", "policy_version",
        "containment_launcher", "argv", "workspace", "child_session_id", "delegation_id",
        "underlying_model_requested", "underlying_model_reported")
    if any(k not in observation for k in required):
        raise ValueError("incomplete parent provenance")
    if observation["requested_worker"] != "cursor" or observation["worker_harness"] != "cursor":
        raise ValueError("wrong harness")
    if any(observation.get(k) != v for k, v in expected.items()):
        raise ValueError("parent provenance mismatch")
    if observation["policy_version"] != POLICY_VERSION or not observation["binary_version"]:
        raise ValueError("missing version or policy fallback")
    if not re.fullmatch(r"[0-9a-f]{64}", observation["binary_sha256"]):
        raise ValueError("invalid binary hash")
    for key in ("resolved_executable", "realpath", "workspace", "containment_launcher"):
        if not isinstance(observation[key], str) or not Path(observation[key]).is_absolute():
            raise ValueError("absolute parent-resolved paths required")
    if not observation["launch_spec_version"] or not observation["child_session_id"] or not observation["delegation_id"]:
        raise ValueError("missing launch identity")
    if not isinstance(observation["argv"], list) or not observation["argv"] or any(
        not isinstance(a, str) for a in observation["argv"]):
        raise ValueError("actual argv required")
    if observation["argv"][0] != observation["containment_launcher"] or observation["resolved_executable"] not in observation["argv"]:
        raise ValueError("binary/containment argv mismatch")


def validate_result(result: dict, *, child_id: str, task_id: int, packet_sha256: str) -> None:
    if not isinstance(result, dict):
        raise ValueError("invalid result")
    if (result.get("child_session_id") != child_id or result.get("task_id") != task_id
        or result.get("packet_sha256") != packet_sha256 or result.get("complete") is not True):
        raise ValueError("wrong identity, missing authority receipt, or incomplete result")
    findings = result.get("findings")
    if not isinstance(findings, list) or not findings or any(
        not isinstance(f, dict) or not isinstance(f.get("file"), str) or not f["file"]
        or type(f.get("line")) is not int or f["line"] <= 0 or not f.get("finding") for f in findings):
        raise ValueError("useful file/line findings required")
    if not result.get("constraints") or not result.get("rejected_approaches"):
        raise ValueError("history receipt missing")


def admission_yes(observations: dict) -> bool:
    """Every control must have run with supervisor evidence. SKIP is never YES."""
    return isinstance(observations, dict) and set(observations) == set(CHECKS) and all(
        isinstance(o, dict) and o.get("status") == "PASS" and bool(o.get("evidence"))
        for o in observations.values())
