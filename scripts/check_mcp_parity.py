#!/usr/bin/env python3
"""Does the INSTALLED MCP entrypoint expose what this checkout registers?

A unit test proves the repository registers a tool. It cannot prove the binary
a client actually spawns has it: pipx installs a COPY, and a client session that
started before an install keeps the old catalog for its whole life. On
2026-09-11 that gap read as "delegation was never implemented" from Codex, while
REST had been serving /api/delegations for 20 minutes.

Exits non-zero when the installed catalog is missing tools this checkout
registers, or when the installed MCP and the running REST report different
commits.
"""
from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
import urllib.request

ATS_MCP = os.environ.get("ATS_MCP_BIN", "/home/patrick/.local/bin/ats-mcp")
SERVER = os.environ.get("ATS_SERVER_URL", "http://localhost:8400")


def installed_catalog() -> tuple[set[str], dict]:
    env = dict(os.environ, ATS_AGENT="parity-check", ATS_SERVER_URL=SERVER)
    p = subprocess.Popen([ATS_MCP], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                         stderr=subprocess.DEVNULL, text=True, env=env)

    def send(o): p.stdin.write(json.dumps(o) + "\n"); p.stdin.flush()

    send({"jsonrpc": "2.0", "id": 1, "method": "initialize",
          "params": {"protocolVersion": "2024-11-05", "capabilities": {},
                     "clientInfo": {"name": "parity", "version": "1"}}})
    info = json.loads(p.stdout.readline())["result"]["serverInfo"]
    send({"jsonrpc": "2.0", "method": "notifications/initialized"})
    send({"jsonrpc": "2.0", "id": 2, "method": "tools/list"})
    names = {t["name"] for t in json.loads(p.stdout.readline())["result"]["tools"]}
    send({"jsonrpc": "2.0", "id": 3, "method": "tools/call",
          "params": {"name": "ats_version", "arguments": {}}})
    ver = json.loads(p.stdout.readline())["result"]["content"][0]["text"]
    p.terminate()
    return names, {"serverInfo": info, "version_text": ver}


def main() -> int:
    from ai_team_sync.mcp.server import list_tools
    expected = {t.name for t in asyncio.run(list_tools())}
    got, meta = installed_catalog()

    missing = sorted(expected - got)
    print(f"installed MCP: {ATS_MCP}")
    print(f"  serverInfo : {meta['serverInfo']}")
    print(f"  tools      : {len(got)} (checkout registers {len(expected)})")
    for line in meta["version_text"].splitlines():
        if line.strip():
            print(f"  {line.strip()}")

    if missing:
        print(f"\nFAIL: installed catalog is missing {missing}")
        print("The deployed entrypoint is stale — run scripts/deploy.sh.")
        return 1

    try:
        with urllib.request.urlopen(f"{SERVER}/api/version", timeout=5) as r:
            rest = json.load(r)
        print(f"  REST commit: {rest['commit']}")
        if "unknown" not in rest["commit"] and rest["commit"] not in meta["version_text"]:
            print("\nFAIL: installed MCP and running REST report different commits.")
            return 1
    except Exception as exc:  # noqa: BLE001
        print(f"  REST version unreachable ({exc}); commit parity NOT checked")

    print("\nOK: installed MCP exposes every registered tool.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
