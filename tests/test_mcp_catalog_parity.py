"""The MCP catalog must cover the contract REST exposes.

Observed 2026-09-11: REST served /api/delegations from 14:01 while an
independent Codex session, whose stdio MCP process started at 13:45, saw no
delegation tools at all. The conclusion drawn was "delegation is not
implemented". Nothing in either surface could state its own revision, so a
deployment-staleness problem was indistinguishable from a missing feature.

These tests hold the repository side of that: a REST contract without its MCP
operations is a failure, not a gap to be discovered by an agent at runtime.
The INSTALLED side is checked by scripts/check_mcp_parity.py, which unit tests
cannot do because pipx installs a copy.
"""

from __future__ import annotations

import pytest

from ai_team_sync.build_info import identity
from ai_team_sync.mcp.server import list_tools
from ai_team_sync.server import create_app


async def _tool_names() -> set[str]:
    return {t.name for t in await list_tools()}


def _rest_paths() -> set[str]:
    """From the OpenAPI schema, not app.routes.

    This FastAPI keeps included routers as _IncludedRouter objects rather than
    flattening their routes, so walking app.routes finds only /health and the
    docs. The schema is also the right source: it is the contract clients see.
    """
    return set(create_app().openapi().get("paths", {}))


@pytest.mark.asyncio
async def test_rest_delegation_routes_have_mcp_operations():
    rest = _rest_paths()
    assert any("/delegations" in p for p in rest), "REST must expose delegations"

    tools = await _tool_names()

    # create, reconcile, and the authority a worker is operating under.
    assert "delegate" in tools
    assert "reconcile_delegation" in tools
    assert "my_authority" in tools


@pytest.mark.asyncio
async def test_the_contract_surface_stays_registered():
    """A named list, so losing a registration fails here and not in an agent."""
    required = {
        "start_session", "complete_session", "extend_scope", "check_locks",
        "team_status", "log_decision", "task_brief", "my_authority",
        "delegate", "reconcile_delegation", "ats_version",
    }

    missing = required - await _tool_names()

    assert not missing, f"MCP catalog lost: {sorted(missing)}"


@pytest.mark.asyncio
async def test_every_tool_is_reachable_by_the_dispatcher():
    """A Tool declared but never handled is worse than an absent one: a client
    sees it advertised and gets a generic failure when it calls."""
    import ai_team_sync.mcp.server as mcp

    source = open(mcp.__file__, encoding="utf-8").read()
    for name in await _tool_names():
        assert f'name == "{name}"' in source, f"{name} is advertised but never handled"


def test_rest_reports_its_build_identity():
    assert "/api/version" in _rest_paths()

    ident = identity("ats-rest")

    assert ident["commit"], "a build with no identity cannot be compared to another"
    assert ident["component"] == "ats-rest"
    assert ident["package_path"]
    assert ident["pid"]


@pytest.mark.asyncio
async def test_machine_facing_ids_are_not_truncated():
    """An id a client must act on cannot be an 8-character prefix: recovering
    the rest by scraping the database is not an interface."""
    import ai_team_sync.mcp.server as mcp

    source = open(mcp.__file__, encoding="utf-8").read()
    offenders = [ln.strip() for ln in source.splitlines()
                 if "[:8]" in ln and "id" in ln.lower()]

    assert not offenders, f"truncated ids in MCP output: {offenders}"
