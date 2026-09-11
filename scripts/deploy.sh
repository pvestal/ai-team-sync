#!/usr/bin/env bash
# Deploy ATS: stamp the commit into the package, install it, restart REST.
#
# The stamp is the point. pipx copies the source tree, so whatever is in
# _build_stamp.json at install time is what the installed copy will report
# forever after — which is how a client can ask "what revision are you" instead
# of inferring it from file timestamps.
set -euo pipefail
cd "$(dirname "$0")/.."

COMMIT="$(git rev-parse --short HEAD)"
DIRTY=false; [ -n "$(git status --porcelain)" ] && DIRTY=true
cat > src/ai_team_sync/_build_stamp.json <<JSON
{"commit": "${COMMIT}", "dirty": ${DIRTY}, "built_at": "$(date -Is)"}
JSON
echo "stamped ${COMMIT} (dirty=${DIRTY})"

pipx install --force . >/dev/null
systemctl --user restart ats-server
sleep 2
curl -s http://localhost:8400/api/version | python3 -m json.tool

echo
echo "NOTE: a running client's stdio MCP keeps the catalog it started with."
echo "Restart Codex / Claude Code sessions to pick this up."
scripts/check_mcp_parity.py
