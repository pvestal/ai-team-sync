"""Bounded event metadata from the existing system journal."""

import json
import re
import uuid

from . import remote_tower

UNITS = ("anime-studio.service", "tower-echo-brain.service",
         "tower-story-render-feeder.service", "comfyui.service", "comfyui-rocm.service")
EVENT_CODES = frozenset({
    "DISPATCH_HELD", "ASSIGNMENT_ROUTE_UNSUPPORTED", "QC_UNAVAILABLE",
    "TECHNICAL_REJECTED", "CUT_NOT_CURRENT", "NO_COMPLETION_RECORDED",
    "ACT_NOT_CANONICAL", "IDENTITY_NOT_READY", "PRODUCTION_LINKAGE_MISSING",
    "COMFYUI_TIMEOUT", "STALE_ARTIFACT", "GRAPH_HASH_MISMATCH",
})
CODE_PATTERN = re.compile(r"\b(?:" + "|".join(sorted(EVENT_CODES)) + r")\b")
TOOL = remote_tower._tool(
    "service_log_summary",
    "Read bounded journal event metadata for one Tower service, including known production reason codes.",
    {"unit": {"type": "string", "enum": list(UNITS)},
     "minutes": {"type": "integer", "minimum": 1, "maximum": 60},
     "limit": {"type": "integer", "minimum": 1, "maximum": 20}}, ["unit"],
)
RELEASE_UNITS = (*UNITS, "ats-server.service", "ats-mcp-remote.service")
RELEASE_TOOL = remote_tower._tool(
    "release_evidence",
    "Read the structured ATS verdict for a review ID, or recent recorded restarts for one service.",
    {"review_id": {"type": "string", "format": "uuid"},
     "unit": {"type": "string", "enum": list(RELEASE_UNITS)},
     "limit": {"type": "integer", "minimum": 1, "maximum": 10}}, [],
)
_SHA = re.compile(r"[a-f0-9]{7,64}")


def _revision(value):
    return value if isinstance(value, str) and _SHA.fullmatch(value) else None


async def release_evidence(client, ats_url: str, arguments: dict):
    review_id, unit = arguments.get("review_id"), arguments.get("unit")
    if bool(review_id) == bool(unit) or (review_id and "limit" in arguments):
        raise remote_tower.BadArgument("Supply either review_id or unit (optionally with limit).")
    if review_id:
        response = await client.get(f"{ats_url}/api/delegations/{uuid.UUID(review_id)}")
        response.raise_for_status()
        row = response.json()
        verdict = row.get("structured_verdict") or {}
        reviewed = verdict.get("reviewed") or {}
        return remote_tower._out("ATS review", {
            **{key: row.get(key) for key in ("id", "state", "mode", "effective_verdict", "closed_at")},
            "base_sha": _revision(reviewed.get("base_sha")),
            "head_sha": _revision(reviewed.get("head_sha")),
            "items": [{key: item.get(key) for key in ("status", "severity", "claim_category")}
                      for item in (verdict.get("items") or [])[:30] if isinstance(item, dict)],
        })
    response = await client.get(f"{ats_url}/api/restarts",
                                params={"unit": unit, "limit": arguments.get("limit", 5)})
    response.raise_for_status()
    rows = response.json()
    return remote_tower._out("ATS restarts", [
        {**{key: row.get(key) for key in ("id", "unit", "outcome", "old_pid", "new_pid", "created_at")},
         "loaded_revision": _revision((row.get("after") or {}).get("loaded_commit"))}
        for row in rows[:10]
    ])


def log_projection(raw: str) -> list[dict]:
    entries = []
    for line in raw.splitlines():
        try:
            row = json.loads(line)
        except ValueError:
            continue
        if not isinstance(row, dict):
            continue
        timestamp, priority = row.get("__REALTIME_TIMESTAMP"), row.get("PRIORITY")
        if not isinstance(timestamp, str) or not re.fullmatch(r"[0-9]{1,18}", timestamp):
            continue
        message = row.get("MESSAGE")
        entries.append({
            "timestamp_us": int(timestamp),
            "priority": int(priority) if isinstance(priority, str) and priority in "01234567"
            and len(priority) == 1 else None,
            "reason_codes": sorted(set(CODE_PATTERN.findall(message))) if isinstance(message, str) else [],
        })
    return entries


async def service_log_summary(arguments: dict):
    unit = arguments["unit"]
    minutes, limit = arguments.get("minutes", 15), arguments.get("limit", 10)
    raw = await remote_tower.run_fixed((
        "journalctl", "--no-pager", "--reverse", "--unit", unit, "--since", f"-{minutes} minutes",
        "--lines", str(limit), "--output", "json",
        "--output-fields", "__REALTIME_TIMESTAMP,PRIORITY,MESSAGE",
    ))
    return remote_tower._out("Service journal metadata", {
        "unit": unit, "minutes": minutes, "limit": limit,
        "possibly_truncated": len(raw) >= remote_tower.FIXED_OUTPUT_LIMIT,
        "events": log_projection(raw)[:limit],
    })
