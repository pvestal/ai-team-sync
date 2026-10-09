"""Read-only data clients for ATS, Echo Brain, Tower, and local status."""

from __future__ import annotations

import asyncio
import os
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import httpx

from . import runtime as runtime_proof
from .sanitize import safe_text, safe_value


@dataclass(frozen=True)
class ConsoleConfig:
    ats_url: str = "http://127.0.0.1:8400"
    echo_url: str = "http://127.0.0.1:8309"
    repo_root: str = ""
    timeout_seconds: float = 3.0
    history_limit: int = 250

    @classmethod
    def from_environment(cls, repo_root: str = "") -> "ConsoleConfig":
        return cls(
            ats_url=os.getenv("ATS_URL", "http://127.0.0.1:8400").rstrip("/"),
            echo_url=os.getenv("ECHO_BRAIN_URL", "http://127.0.0.1:8309").rstrip("/"),
            repo_root=repo_root or os.getcwd(),
        )


class ReadOnlySources:
    """The console's only I/O facade. Every HTTP request here is GET."""

    def __init__(self, config: ConsoleConfig, client: httpx.AsyncClient | None = None):
        self.config = config
        self._owns_client = client is None
        self.client = client or httpx.AsyncClient(timeout=config.timeout_seconds)

    async def close(self) -> None:
        if self._owns_client:
            await self.client.aclose()

    async def _get(self, base: str, path: str, params: dict | None = None) -> dict:
        response = await self.client.get(f"{base}{path}", params=params)
        response.raise_for_status()
        value = response.json()
        return safe_value(value) if isinstance(value, dict) else {"value": safe_value(value)}

    async def ats_sessions(self, limit: int = 100) -> dict:
        return await self._get(
            self.config.ats_url, "/api/observability/sessions", {"limit": min(limit, 200)}
        )

    async def ats_events(
        self,
        *,
        cursor: str | None = None,
        session_id: str | None = None,
        task_id: int | None = None,
        limit: int | None = None,
    ) -> dict:
        params: dict[str, Any] = {"limit": min(limit or self.config.history_limit, 500)}
        if cursor:
            params["cursor"] = cursor
        if session_id:
            params["session_id"] = session_id
        if task_id is not None:
            params["task_id"] = task_id
        return await self._get(self.config.ats_url, "/api/observability/events", params)

    async def ats_event_detail(self, event_id: str) -> dict:
        return await self._get(
            self.config.ats_url,
            "/api/observability/events",
            {"limit": 1, "event_id": event_id, "include_detail": "true"},
        )

    async def coverage(self) -> dict:
        return await self._get(self.config.ats_url, "/api/observability/coverage")

    async def echo_summary(self) -> dict:
        """Fetch bounded metadata only: never memory contents or raw tool output."""

        async def guarded(path: str, params: dict | None = None) -> dict:
            try:
                return {
                    "state": "OBSERVED",
                    "data": await self._get(self.config.echo_url, path, params),
                }
            except (httpx.HTTPError, ValueError) as exc:
                return {"state": "DISCONNECTED", "error": type(exc).__name__}

        health, preflight, telemetry = await asyncio.gather(
            guarded("/health"),
            guarded("/api/preflight", {"limit": 20}),
            guarded("/api/echo/telemetry/mcp", {"hours": 1, "limit": 20}),
        )
        return {"health": health, "preflight": preflight, "telemetry": telemetry}

    async def tower_task(self, task_id: int) -> dict:
        try:
            raw = await self._get(self.config.echo_url, f"/api/tower-tasks/{task_id}")
        except httpx.HTTPStatusError as exc:
            return {"state": "UNAVAILABLE", "status_code": exc.response.status_code}
        except (httpx.HTTPError, ValueError) as exc:
            return {"state": "DISCONNECTED", "error": type(exc).__name__}
        # Deliberate allowlist. Full task prose is authority-bearing context and is
        # not copied into the general console surface in Phase 1.
        claim_value = raw.get("claim")
        relations_value = raw.get("relations")
        claim: dict[str, Any] = claim_value if isinstance(claim_value, dict) else {}
        relations: dict[str, Any] = relations_value if isinstance(relations_value, dict) else {}
        return {
            "state": "OBSERVED",
            "id": raw.get("id"),
            "task_key": raw.get("task_key"),
            "project_id": raw.get("project_id"),
            "project_name": raw.get("project_name"),
            "title": safe_text(raw.get("title"), 240),
            "status": raw.get("status"),
            "gate": raw.get("gate"),
            "priority": raw.get("priority"),
            "blocked_by": safe_value(raw.get("blocked_by") or []),
            "updated_at": raw.get("updated_at"),
            "verified_by": safe_value(raw.get("verified_by")),
            "claim": {
                key: safe_value(claim.get(key))
                for key in ("id", "state", "executor", "session_id", "lease_expires_at")
            },
            "active_residuals": safe_value(relations.get("active_residuals") or []),
            "description": "WITHHELD_BY_DEFAULT",
        }

    async def echo_preflight_provenance(self, request_id: str) -> dict:
        """Expand provenance fields, never evidence or memory content."""
        try:
            raw = await self._get(self.config.echo_url, f"/api/preflight/{request_id}")
        except httpx.HTTPStatusError as exc:
            return {"state": "UNAVAILABLE", "status_code": exc.response.status_code}
        except (httpx.HTTPError, ValueError) as exc:
            return {"state": "DISCONNECTED", "error": type(exc).__name__}
        evidence_value = raw.get("evidence")
        evidence = evidence_value if isinstance(evidence_value, list) else []
        return {
            "state": "OBSERVED",
            "request_id": request_id,
            "provenance": [
                {
                    key: safe_value(row.get(key))
                    for key in (
                        "id",
                        "source_type",
                        "source_ref",
                        "citation",
                        "authority_class",
                        "authority_rank",
                        "relation",
                        "match_type",
                        "semantic_score",
                        "state",
                        "occurred_at",
                        "revision",
                    )
                }
                for row in evidence[:100]
                if isinstance(row, dict)
            ],
            "content": "WITHHELD_BY_DEFAULT",
        }

    async def runtime(self, sessions: list[dict]) -> dict:
        """Process-level proof of which runtime backs each session (read-only /proc)."""
        try:
            return await asyncio.to_thread(runtime_proof.runtime_snapshot, sessions)
        except Exception as exc:  # noqa: BLE001 - a probe failure must not take the console down
            return {"state": "UNAVAILABLE", "error": type(exc).__name__, "tree": [], "verdicts": {}}

    async def local_status(self) -> dict:
        repo = str(Path(self.config.repo_root).resolve())
        probes = _local_probes(repo)
        results = await asyncio.gather(*(_run_probe(name, argv) for name, argv in probes.items()))
        return {name: result for name, result in results}

    @staticmethod
    def echo_events(summary: dict) -> list[dict]:
        block = summary.get("preflight", {})
        recent = block.get("data", {}).get("recent", []) if block.get("state") == "OBSERVED" else []
        events = []
        for row in recent[:20]:
            timestamp = row.get("created_at") or datetime.now(timezone.utc).isoformat()
            events.append(
                {
                    "id": f"echo-preflight:{row.get('id')}",
                    "timestamp": timestamp,
                    "event_type": "ECHO",
                    "integrity": "PARTIAL",
                    "session_id": None,
                    "task_id": None,
                    "source": {"session_id": None, "agent": row.get("requesting_worker")},
                    "target": {"session_id": None, "agent": "echo"},
                    "summary": safe_text(row.get("objective") or "Echo preflight", 160),
                    "status": row.get("disposition"),
                    "source_record": f"preflight_requests:{row.get('id')}",
                    "detail": {
                        "operation_type": row.get("operation_type"),
                        "evidence_count": row.get("evidence_count"),
                        "operator_evidence": row.get("operator_evidence"),
                        "semantic_used": row.get("semantic_used"),
                        "model_used": row.get("model_used"),
                        "duration_ms": row.get("duration_ms"),
                        "content": "SOURCE_METADATA_ONLY",
                    },
                }
            )
        return events


def _local_probes(repo: str) -> dict[str, list[str]]:
    """Fixed read-only argv for local status.

    ATS is installed as a user service named ``ats-server``. The remaining
    Tower services are system services. Keeping the distinction here prevents
    a healthy ATS process from being rendered as unavailable merely because a
    different, nonexistent system unit was queried.
    """
    return {
        "git": ["git", "-C", repo, "status", "--short", "--branch"],
        "ats-service": ["systemctl", "--user", "is-active", "ats-server"],
        "echo-service": ["systemctl", "is-active", "tower-echo-brain.service"],
        "comfyui-3060": ["systemctl", "is-active", "comfyui.service"],
        "comfyui-rocm": ["systemctl", "is-active", "comfyui-rocm.service"],
        "nvidia": [
            "nvidia-smi",
            "--query-gpu=name,utilization.gpu,memory.used,memory.total",
            "--format=csv,noheader,nounits",
        ],
        "amd": ["rocm-smi", "--showuse", "--showmemuse"],
    }


async def _run_probe(name: str, argv: list[str]) -> tuple[str, dict]:
    """Run a fixed argv read probe with no shell and a strict output/time cap."""
    try:
        process = await asyncio.create_subprocess_exec(
            *argv, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
        )
        stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=2.5)
    except FileNotFoundError:
        return name, {"state": "UNAVAILABLE", "summary": f"{argv[0]} not installed"}
    except asyncio.TimeoutError:
        try:
            process.kill()
        except ProcessLookupError:
            pass
        return name, {"state": "UNAVAILABLE", "summary": "probe timed out"}
    output = safe_text((stdout + stderr).decode(errors="replace"), 2400)
    return name, {
        "state": "OBSERVED" if process.returncode == 0 else "UNAVAILABLE",
        "returncode": process.returncode,
        "summary": _probe_summary(name, output),
    }


def _probe_summary(name: str, output: str) -> str:
    """Convert command output to bounded metrics; never render raw tool output."""
    lines = [line.strip() for line in output.splitlines() if line.strip()]
    if name == "git":
        branch = lines[0].removeprefix("## ") if lines else "unknown"
        return f"branch={branch}; changed_paths={max(0, len(lines) - 1)}"
    if name == "nvidia":
        summaries = []
        for index, line in enumerate(lines[:8]):
            parts = [part.strip() for part in line.split(",")]
            if len(parts) >= 4:
                summaries.append(
                    f"gpu{index}={parts[0]}; util={parts[1]}%; vram={parts[2]}/{parts[3]} MiB"
                )
        return "; ".join(summaries) or "metrics unavailable"
    if name == "amd":
        values: dict[str, dict[str, str]] = {}
        for line in lines:
            match = re.search(r"GPU\[(\d+)]\s*:\s*GPU use \(%\):\s*(\d+)", line)
            if match:
                values.setdefault(match.group(1), {})["util"] = match.group(2)
            match = re.search(r"GPU\[(\d+)]\s*:\s*GPU Memory Allocated \(VRAM%\):\s*(\d+)", line)
            if match:
                values.setdefault(match.group(1), {})["vram"] = match.group(2)
        return (
            "; ".join(
                f"gpu{gpu}: util={item.get('util', '—')}%; vram={item.get('vram', '—')}%"
                for gpu, item in sorted(values.items())
            )
            or "metrics unavailable"
        )
    # systemctl probes contain a single state word; errors are bounded and sanitized.
    return safe_text(lines[0] if lines else "no output", 200)
