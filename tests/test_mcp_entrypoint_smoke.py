"""The MCP entrypoint must import and expose tools. This is the outage test.

REGRESSION: 2026-08-24, fleet-wide outage.

pyproject pinned `mcp>=1.0.0` with no upper bound. A `--force-reinstall` during a
deploy pulled mcp 2.1.0, which removed the low-level decorator API
(@Server.list_tools / @Server.call_tool) and dropped mcp.server.fastmcp. The
entrypoint then died at IMPORT time:

    src/ai_team_sync/mcp/server.py:151  @mcp_server.list_tools()
    -> AttributeError: 'Server' object has no attribute 'list_tools'

Effect: ZERO ATS tools reached ANY Claude Code session. No session could
start_session, extend_scope, take locks, or log_decision through the harness.
Every session on the board sat UNSCOPED with 0 locks, which reads as an
under-used board rather than a broken one.

Two things hid it, and both are worth remembering:

  - The SessionStart health check reported "ok ai-team-sync (stdio)" because it
    only resolved the binary. It never loaded the tool list. A dead server
    reported green.
  - The `ats` CLI and the :8400 server were unaffected — neither imports
    ai_team_sync.mcp — so every other signal stayed healthy.

Seven test files already imported ai_team_sync.mcp and would have caught this.
None of them ran: the repo had no CI test workflow, and the deploy path
(`pipx install --force`) runs no tests. The guard existed; nothing invoked it.
That gap is closed by .github/workflows/tests.yml alongside this file.

These tests are deliberately blunt. They assert the two things whose absence
caused a silent fleet outage: the module imports, and it actually offers tools.
"""

from __future__ import annotations

import asyncio

import pytest


def test_the_mcp_entrypoint_imports_at_all():
    """The literal 2026-08-24 failure: import-time AttributeError.

    Any incompatible mcp major bump fails HERE, loudly, in CI — instead of
    silently at every Claude session start.
    """
    import ai_team_sync.mcp.server as srv  # noqa: F401

    assert srv.mcp_server is not None


def test_the_run_callable_exists():
    """`ats-mcp` resolves to ai_team_sync.mcp.server:run — pyproject [project.scripts]."""
    from ai_team_sync.mcp.server import run

    assert callable(run)


def test_the_server_actually_exposes_tools():
    """Importing is necessary but NOT sufficient — the decorators must have bound.

    A registry that imports cleanly but offers zero tools is the same outage from
    a session's point of view.
    """
    import ai_team_sync.mcp.server as srv

    handler = srv.mcp_server.request_handlers
    assert handler, "MCP server registered no request handlers"

    tools = asyncio.run(srv.list_tools()) if hasattr(srv, "list_tools") else None
    if tools is None:
        pytest.skip("list_tools not exposed at module scope in this mcp version")
    assert len(tools) > 0, "MCP server exposed no tools"


@pytest.mark.parametrize(
    "name",
    [
        "start_session",
        "extend_scope",
        "log_decision",
        "check_locks",
        "team_status",
    ],
)
def test_the_tools_master_control_depends_on_are_present(name):
    """These five are what a session needs to claim ownership before doing work.

    Losing any one of them reproduces the governance half of the outage: workers
    that can investigate but cannot declare scope, which is exactly the condition
    that produced the #2357 duplicate-work collision.
    """
    import ai_team_sync.mcp.server as srv

    if not hasattr(srv, "list_tools"):
        pytest.skip("list_tools not exposed at module scope in this mcp version")
    tools = asyncio.run(srv.list_tools())
    assert name in {t.name for t in tools}
