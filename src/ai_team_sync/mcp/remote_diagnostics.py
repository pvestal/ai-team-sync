"""Bounded event metadata from the existing system journal."""

import json
import re

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
