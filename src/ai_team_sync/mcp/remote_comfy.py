"""Metadata projections for the two fixed Tower ComfyUI backends."""

import hashlib
import json
import uuid

from . import remote_tower

BACKENDS = {"nvidia": 8188, "amd": 8189}
NODE_CLASSES = frozenset({
    "CLIPTextEncode", "CLIPVisionEncode", "ControlNetApplyAdvanced", "ControlNetLoader",
    "DepthAnythingV2Preprocessor", "IPAdapterAdvanced", "KSampler", "KSamplerAdvanced",
    "LoadImage", "LoadImageMask", "LoraLoader", "LoraLoaderModelOnly", "OpenposePreprocessor",
    "SaveImage", "UnetLoaderGGUF", "VAEDecode", "VHS_VideoCombine",
    "WanFirstLastFrameToVideo", "WanImageToVideo", "Wan22FunControlToVideo",
})
TOOL = remote_tower._tool(
    "comfy_execution_metadata",
    "Current queue counts, bounded recent execution status, submitted graph digests "
    "and selected node availability for one Tower backend.",
    {"backend": {"type": "string", "enum": list(BACKENDS)}}, ["backend"],
)


def _history_projection(history: dict) -> list:
    result = []
    for raw_id, row in list(history.items())[:5]:
        try:
            prompt_id = str(uuid.UUID(raw_id))
        except (TypeError, ValueError):
            continue
        prompt = row.get("prompt") or []
        if len(prompt) < 3 or not isinstance(prompt[2], dict):
            continue
        graph = prompt[2]
        classes = {node.get("class_type") for node in graph.values()
                   if isinstance(node, dict) and isinstance(node.get("class_type"), str)}
        status_data = row.get("status") or {}
        status = status_data.get("status_str")
        started = finished = None
        failure = None
        for event in (status_data.get("messages") or [])[:100]:
            if not isinstance(event, list) or len(event) != 2 or not isinstance(event[1], dict):
                continue
            kind, payload = event
            timestamp = payload.get("timestamp")
            if type(timestamp) is not int or not 0 < timestamp < 10**14:
                continue
            if kind == "execution_start":
                started = timestamp
            if kind in ("execution_success", "execution_error", "execution_interrupted"):
                finished = timestamp
                if kind != "execution_success":
                    node = payload.get("node_type")
                    failure = {"event": kind, "node_class": node if isinstance(node, str)
                               and node in NODE_CLASSES else None}
        graph_bytes = json.dumps(graph, ensure_ascii=False, sort_keys=True,
                                 separators=(",", ":")).encode()
        result.append({
            "prompt_id": prompt_id, "status": status if status in ("success", "error") else "unknown",
            "graph_digest": hashlib.sha256(graph_bytes).hexdigest(),
            "node_count": len(graph), "known_node_classes": sorted(classes & NODE_CLASSES),
            "duration_ms": (finished - started
                            if started is not None and finished is not None and started <= finished else None),
            "failure": failure,
        })
    return result


async def execution_metadata(client, arguments):
    import asyncio

    backend = arguments["backend"]
    base = f"http://127.0.0.1:{BACKENDS[backend]}"
    queue, history, nodes = await asyncio.gather(
        client.get(f"{base}/queue"),
        client.get(f"{base}/history", params={"max_items": 5}),
        client.get(f"{base}/object_info/WanImageToVideo"),
    )
    for response in (queue, history, nodes):
        response.raise_for_status()
    queue = queue.json()
    return remote_tower._out("ComfyUI execution metadata", {
        "backend": backend,
        "running_count": len(queue["queue_running"]),
        "pending_count": len(queue["queue_pending"]),
        "recent": _history_projection(history.json()),
        "installed_node_classes": sorted({"WanImageToVideo"} & nodes.json().keys()),
    })
