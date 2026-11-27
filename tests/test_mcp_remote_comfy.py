import httpx
import pytest

from ai_team_sync.mcp import remote_readonly as connector
from ai_team_sync.mcp.remote_comfy import _history_projection


@pytest.mark.parametrize("method,url,allowed", [
    ("GET", "http://127.0.0.1:8188/queue", True),
    ("GET", "http://127.0.0.1:8189/history?max_items=5", True),
    ("GET", "http://127.0.0.1:8189/object_info/WanImageToVideo", True),
    ("GET", "http://127.0.0.1:8189/history?max_items=50", False),
    ("POST", "http://127.0.0.1:8189/queue", False),
    ("POST", "http://127.0.0.1:8189/prompt", False),
    ("GET", "http://127.0.0.1:8190/queue", False),
    ("GET", "http://127.0.0.1:8189/view", False),
])
def test_comfy_transport_is_pinned(method, url, allowed):
    assert connector.upstream_allowed(method, httpx.URL(url)) is allowed


def test_history_projection_excludes_inputs_and_outputs():
    row = {"prompt": [0, "id", {
        "1": {"class_type": "LoadImage", "inputs": {"image": "PRIVATE"}},
        "2": {"class_type": "PRIVATE_CLASS", "inputs": {"text": "PRIVATE"}},
    }], "status": {"status_str": "error", "messages": ["PRIVATE"]},
        "outputs": {"1": {"images": [{"filename": "PRIVATE"}]}}}
    projected, = _history_projection({"c6ae78d7-cbc1-4b1c-af70-1aed03ce83dd": row})
    assert projected["known_node_classes"] == ["LoadImage"]
    assert projected["node_count"] == 2
    assert projected["status"] == "error"
    assert len(projected["graph_digest"]) == 64
    assert "PRIVATE" not in repr(projected)


def test_journal_projection_exposes_reason_codes_not_message_text():
    from ai_team_sync.mcp.remote_diagnostics import log_projection

    raw = '{"__REALTIME_TIMESTAMP":"1234","PRIORITY":"3","MESSAGE":"private SECRET QC_UNAVAILABLE"}'
    assert log_projection(raw) == [{
        "timestamp_us": 1234, "priority": 3, "reason_codes": ["QC_UNAVAILABLE"],
    }]


async def test_journal_cap_keeps_newest_events(monkeypatch):
    import json
    from ai_team_sync.mcp import remote_diagnostics, remote_tower

    async def journal(argv):
        assert "--reverse" in argv
        raw = "\n".join(json.dumps({
            "__REALTIME_TIMESTAMP": str(index), "PRIORITY": "3",
            "MESSAGE": "x" * 1500 + (" QC_UNAVAILABLE" if index == 20 else ""),
        }) for index in range(20, 0, -1))
        return raw[:remote_tower.FIXED_OUTPUT_LIMIT]

    monkeypatch.setattr(remote_tower, "run_fixed", journal)
    result, = await remote_diagnostics.service_log_summary({"unit": "anime-studio.service", "limit": 20})
    payload = json.loads(result.text.split("\n", 1)[1])
    assert payload["possibly_truncated"]
    assert payload["events"][0]["timestamp_us"] == 20
    assert payload["events"][0]["reason_codes"] == ["QC_UNAVAILABLE"]
