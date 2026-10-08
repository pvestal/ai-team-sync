from __future__ import annotations

import json

import httpx
import pytest

from ai_team_sync.console.sanitize import safe_text, safe_value
from ai_team_sync.console.sources import ConsoleConfig, ReadOnlySources, _probe_summary


def test_sanitizer_removes_terminal_controls_and_common_secrets():
    value = safe_text(
        "\x1b[31mtoken=topsecret sk-abcdefghijklmnop "
        "AKIAABCDEFGHIJKLMNOP ghp_abcdefghijklmnopqrstuvwxyz\x00"
    )
    assert "\x1b" not in value
    assert "topsecret" not in value
    assert "sk-abcdef" not in value
    assert "AKIA" not in value
    assert "ghp_" not in value
    assert value.count("[REDACTED]") == 4


def test_local_probe_summaries_do_not_expose_raw_git_paths():
    summary = _probe_summary("git", "## feature/console\n M private/customer.txt\n?? token.env\n")
    assert summary == "branch=feature/console; changed_paths=2"
    assert "customer" not in summary
    assert "token.env" not in summary

    amd = _probe_summary(
        "amd", "GPU[0] : GPU use (%): 94\nGPU[0] : GPU Memory Allocated (VRAM%): 61"
    )
    assert amd == "gpu0: util=94%; vram=61%"


@pytest.mark.asyncio
async def test_sources_use_get_only_and_allowlist_task_envelope():
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.url.path == "/api/observability/sessions":
            return httpx.Response(200, json={"sessions": []})
        if request.url.path == "/api/observability/events":
            return httpx.Response(200, json={"events": [], "next_cursor": "cursor"})
        if request.url.path == "/api/observability/coverage":
            return httpx.Response(200, json={"event_types": {}})
        if request.url.path == "/api/tower-tasks/3522":
            return httpx.Response(
                200,
                json={
                    "id": 3522,
                    "title": "Access boundary",
                    "description": "private criteria",
                    "status": "pending",
                    "gate": "operator",
                    "blocked_by": [7],
                    "claim": {"state": "open", "executor": "worker", "secret": "no"},
                    "relations": {"active_residuals": [8], "children": [{"secret": "no"}]},
                    "task_context": {"memory": "never expose"},
                },
            )
        if request.url.path == "/api/preflight/req-1":
            return httpx.Response(
                200,
                json={
                    "request": {"objective": "sensitive task text"},
                    "evidence": [
                        {
                            "id": "evidence-1",
                            "source_type": "ats_decision",
                            "source_ref": "decision:7",
                            "citation": "ats-decision/7",
                            "authority_class": "OPERATOR_EVIDENCE",
                            "evidence_text": "private memory body",
                        }
                    ],
                },
            )
        raise AssertionError(request.url)

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    source = ReadOnlySources(ConsoleConfig(ats_url="http://ats", echo_url="http://echo"), client)
    await source.ats_sessions()
    await source.ats_events()
    await source.ats_event_detail("decision:1:created")
    await source.coverage()
    task = await source.tower_task(3522)
    provenance = await source.echo_preflight_provenance("req-1")

    assert all(request.method == "GET" for request in requests)
    assert task["description"] == "WITHHELD_BY_DEFAULT"
    rendered = json.dumps(task)
    assert "private criteria" not in rendered
    assert "never expose" not in rendered
    assert task["active_residuals"] == [8]
    provenance_text = json.dumps(provenance)
    assert "ats-decision/7" in provenance_text
    assert "private memory body" not in provenance_text
    assert "sensitive task text" not in provenance_text
    await client.aclose()


def test_echo_events_are_explicitly_partial_metadata():
    summary = {
        "preflight": {
            "state": "OBSERVED",
            "data": {
                "recent": [
                    {
                        "id": "req-1",
                        "requesting_worker": "codex",
                        "objective": "review task",
                        "disposition": "PROCEED",
                        "created_at": "2026-10-07T12:00:00+00:00",
                        "evidence_count": 4,
                        "operator_evidence": 2,
                        "semantic_used": True,
                        "model_used": False,
                        "duration_ms": 91,
                    }
                ]
            },
        }
    }
    event = ReadOnlySources.echo_events(safe_value(summary))[0]
    assert event["integrity"] == "PARTIAL"
    assert event["source"]["agent"] == "codex"
    assert event["target"]["agent"] == "echo"
    assert event["detail"]["content"] == "SOURCE_METADATA_ONLY"
