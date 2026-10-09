from __future__ import annotations

import pytest
from textual.widgets import DataTable, ListView, Static

from ai_team_sync.console.app import TowerConsole
from ai_team_sync.console.sources import ConsoleConfig


class FakeSources:
    async def ats_sessions(self, limit=100):
        return {
            "lineage_truncated": True,
            "sessions": [
                {
                    "id": "session-one",
                    "agent": "codex:test",
                    "status": "active",
                    "task_id": 3522,
                    "effective_mode": "READ_ONLY",
                    "scope": [],
                    "parent_session_id": None,
                    "child_session_ids": [],
                    "effective_authority": {"edit": "none"},
                    "ats_health": {
                        "registered": True,
                        "scoped": False,
                        "task": True,
                        "brief": "UNINSTRUMENTED",
                        "client_connection": "UNINSTRUMENTED",
                    },
                }
            ],
        }

    async def ats_events(self, **kwargs):
        return {
            "next_cursor": "cursor",
            "events": [
                {
                    "id": "decision:one:created",
                    "timestamp": "2026-10-07T12:00:00+00:00",
                    "event_type": "RULING",
                    "integrity": "OBSERVED",
                    "session_id": "session-one",
                    "task_id": 3522,
                    "source": {"session_id": "session-one", "agent": "codex:test"},
                    "target": None,
                    "summary": "Read only",
                    "status": "recorded",
                    "source_record": "decisions:one",
                    "detail": {"chosen": "no mutation"},
                }
            ],
        }

    async def ats_event_detail(self, event_id):
        return {"events": []}

    async def runtime(self, sessions):
        return {
            "state": "OBSERVED",
            "tree": [
                {
                    "pid": 10,
                    "runtime": "codex",
                    "argv": "codex exec",
                    "via": ["bash", "subagent:adversarial-reviewer"],
                    "subagent": None,
                    "session": {
                        "id": "session-one",
                        "agent": "codex:test",
                        "task_id": 3522,
                        "mode": "READ_ONLY",
                        "parent": None,
                        "verdict": "VERIFIED",
                    },
                    "children": [],
                }
            ],
            "verdicts": {"session-one": {"state": "VERIFIED", "pid": 10}},
            "lineage_gaps": [],
        }

    async def echo_summary(self):
        return {
            "health": {"state": "OBSERVED", "data": {"status": "ok"}},
            "preflight": {"state": "OBSERVED", "data": {"recent": []}},
            "telemetry": {
                "state": "OBSERVED",
                "data": {
                    "rows": [
                        {
                            "tool_name": "search_memory",
                            "calls": 4,
                            "avg_ms": 22,
                            "p95_ms": 40,
                            "errs": 0,
                            "last_call": "now",
                        }
                    ]
                },
            },
        }

    @staticmethod
    def echo_events(summary):
        return []

    async def local_status(self):
        return {
            "git": {"state": "OBSERVED", "summary": "clean"},
            "nvidia": {"state": "UNAVAILABLE", "summary": "not installed"},
        }

    async def tower_task(self, task_id):
        return {
            "state": "OBSERVED",
            "id": task_id,
            "status": "pending",
            "description": "WITHHELD_BY_DEFAULT",
        }

    async def echo_preflight_provenance(self, request_id):
        return {
            "state": "OBSERVED",
            "provenance": [],
            "content": "WITHHELD_BY_DEFAULT",
        }

    async def close(self):
        return None


@pytest.mark.asyncio
async def test_app_bootstraps_multi_pane_exchange_and_filters():
    app = TowerConsole(ConsoleConfig(), sources=FakeSources())
    async with app.run_test(size=(140, 45)) as pilot:
        assert app.query_one("#agent-list", ListView).children
        assert app.query_one("#exchange", DataTable).row_count == 1
        assert app.query_one("#tool-table", DataTable).row_count == 1
        assert "OBSERVED" in str(app.query_one("#system", Static).render())
        assert app.query_one("#detail", Static).markup is False
        assert app.query_one("#task", Static).markup is False
        assert app.query_one("#system", Static).markup is False
        assert app.connectivity["ATS"] == "PARTIAL"

        await pilot.press("f")
        assert app.filters["type"] == "ATS"
        assert app.query_one("#exchange", DataTable).row_count == 0
        await pilot.press("f")
        assert app.filters["type"] == "HANDOFF"


@pytest.mark.asyncio
async def test_app_uses_compact_layout_on_narrow_terminal():
    app = TowerConsole(ConsoleConfig(), sources=FakeSources())
    async with app.run_test(size=(90, 30)):
        assert app.has_class("narrow")


@pytest.mark.asyncio
async def test_app_shows_runtime_chain_and_marks_verified_agents():
    app = TowerConsole(ConsoleConfig(), sources=FakeSources())
    async with app.run_test(size=(160, 50)):
        runtime = str(app.query_one("#runtime", Static).render())
        assert "via Claude subagent:adversarial-reviewer" in runtime
        assert "CODEX pid 10" in runtime and "codex:test" in runtime
        assert app.query_one("#runtime", Static).markup is False
        assert app.connectivity["Proc"] == "OBSERVED"
        row = app.query_one("#agent-list", ListView).children[0]
        assert "pid✓" in str(row.query_one("Label").render())
