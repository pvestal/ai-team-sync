"""Shared lifecycle-session registration for supported agent clients.

Platform adapters are responsible only for mapping their hook payload into
``RegistrationInput``.  This module owns the ATS semantics so Claude and Codex
reuse, resurrection, and replacement behavior cannot drift.
"""

from __future__ import annotations

import hashlib
import os
import subprocess
from dataclasses import dataclass
from typing import Any

from ai_team_sync import session_pointer as sp
from ai_team_sync.context_resolution import governed_roots, resolve_request_target
from ai_team_sync.session_marker import AUTOREG_DESCRIPTION


@dataclass(frozen=True)
class RegistrationInput:
    lifecycle_session_id: str
    agent: str
    cwd: str
    hook_event_name: str = ""
    model: str = ""
    source: str = ""


class RegistrationError(RuntimeError):
    """An existing lifecycle row could not be safely reused."""


def lifecycle_session_key(agent: str, lifecycle_session_id: str) -> str:
    """Return the collision-resistant local identity key for one lifecycle.

    Claude session UUIDs historically used their first eight random characters
    for pointer files and display labels. Codex uses UUIDv7-style thread IDs,
    whose leading characters are time ordered: processes started close together
    can share that entire prefix. Hash Codex IDs before passing them into the
    shared pointer/label machinery so its existing eight-character key remains
    compact without aliasing concurrent Codex lifecycles.
    """
    lifecycle_session_id = lifecycle_session_id.strip()
    if lifecycle_session_id and agent.strip().lower() == "codex":
        return hashlib.sha256(lifecycle_session_id.encode("utf-8")).hexdigest()
    return lifecycle_session_id


def developer() -> str:
    if os.environ.get("ATS_DEVELOPER"):
        return os.environ["ATS_DEVELOPER"]
    try:
        name = subprocess.run(
            ["git", "config", "user.name"],
            capture_output=True,
            text=True,
            timeout=2,
        ).stdout.strip()
        if name:
            return name
    except Exception:  # noqa: BLE001 - registration must remain fail-open at startup
        pass
    return os.environ.get("USER", "unknown")


def _description(data: RegistrationInput) -> str:
    # AUTOREG_DESCRIPTION must remain the prefix: start_session uses it to
    # recognize and adopt a scope-less lifecycle placeholder.
    def bounded(value: str, limit: int = 300) -> str:
        value = " ".join(str(value).split())
        return value if len(value) <= limit else value[: limit - 3] + "..."

    facts = [f"client={bounded(data.agent, 40)}", f"cwd={bounded(data.cwd)}"]
    if data.model:
        facts.append(f"model={bounded(data.model, 80)}")
    if data.source:
        facts.append(f"source={bounded(data.source, 80)}")
    if data.hook_event_name:
        facts.append(f"event={bounded(data.hook_event_name, 80)}")
    return f"{AUTOREG_DESCRIPTION}; " + "; ".join(facts)


async def _reuse_existing(
    server_url: str, client: Any, session_id: str, lifecycle_session_id: str
) -> str | None:
    """Reuse an active row or resurrect an auto-reaped row.

    Explicitly completed rows are terminal and return ``None`` so the same
    client lifecycle can receive a fresh placeholder without reviving operator-
    completed work. Unknown rows follow the same replacement path.
    """
    try:
        response = await client.get(f"{server_url}/api/sessions/{session_id}")
    except Exception as exc:  # noqa: BLE001
        raise RegistrationError(f"ATS lookup failed for session {session_id}") from exc
    if response.status_code == 404:
        return None
    if response.status_code != 200:
        raise RegistrationError(
            f"ATS lookup failed for session {session_id}: HTTP {response.status_code}"
        )
    row = response.json()
    if row.get("status") == "active":
        sp.save_pointer(session_id, lifecycle_session_id)
        return session_id
    if row.get("status") == "completed" and row.get("auto_completed") is True:
        try:
            heartbeat = await client.post(f"{server_url}/api/sessions/{session_id}/heartbeat")
        except Exception as exc:  # noqa: BLE001
            raise RegistrationError(
                f"ATS could not resurrect auto-reaped session {session_id}"
            ) from exc
        if heartbeat.status_code == 200 and heartbeat.json().get("status") == "active":
            sp.save_pointer(session_id, lifecycle_session_id)
            return session_id
        raise RegistrationError(
            f"ATS could not resurrect auto-reaped session {session_id}: "
            f"HTTP {heartbeat.status_code}"
        )
    return None


async def ensure_session(server_url: str, client: Any, data: RegistrationInput) -> str | None:
    """Create, reuse, or safely resurrect one lifecycle's ATS session."""
    cid = lifecycle_session_key(data.agent, data.lifecycle_session_id)
    if not cid:
        return None

    # Per-lifecycle pointers only. The legacy global pointer may belong to a
    # concurrent Claude/Codex process and must never transfer identity.
    existing = sp.resolve_pointer(cid, allow_global=False)
    if existing:
        reused = await _reuse_existing(server_url, client, existing, cid)
        if reused:
            return reused

    try:
        target = resolve_request_target("", cwd=data.cwd, governed_roots=governed_roots())
        repo_root = target.repo_root if target else ""
        response = await client.post(
            f"{server_url}/api/sessions",
            json={
                "developer": developer(),
                "agent": sp.agent_label(data.agent, cid),
                "scope": [],
                "description": _description(data),
                "repo_root": repo_root,
                "auto_lock": False,
            },
        )
        if response.status_code in (200, 201):
            sid = response.json()["id"]
            sp.save_pointer(sid, cid)
            sp.save_approval_token(sid, response.headers.get("X-ATS-Approval-Token", ""), cid)
            return sid
    except Exception:  # noqa: BLE001 - callers choose startup/prompt failure policy
        pass
    return None
