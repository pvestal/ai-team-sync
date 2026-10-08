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
cat > src/ai_team_sync/_build_stamp.py <<PY
"""Generated at deploy time. Not source; see .gitignore."""
COMMIT = "${COMMIT}"
DIRTY = ${DIRTY^}
BUILT_AT = "$(date -Is)"
PY
echo "stamped ${COMMIT} (dirty=${DIRTY})"

pipx install --force . >/dev/null

# Harness behavior is part of this deployment, not an optional README step.
# Order is load-bearing: ATS resolves governed context before the Echo ambient
# hook contributes supplemental memory.
ATS_HOOK_PYTHON="${ATS_HOOK_PYTHON:-${HOME}/.local/share/pipx/venvs/ai-team-sync/bin/python}"
python3 scripts/install-claude-hooks.py \
  --settings "${ATS_CLAUDE_SETTINGS:-${HOME}/.claude/settings.json}" \
  --python "${ATS_HOOK_PYTHON}"
python3 scripts/install-codex-hooks.py \
  --hooks "${ATS_CODEX_HOOKS:-${HOME}/.codex/hooks.json}" \
  --config "${ATS_CODEX_CONFIG:-${HOME}/.codex/config.toml}" \
  --python "${ATS_HOOK_PYTHON}"
systemctl --user restart ats-server
sleep 2
curl -s http://localhost:8400/api/version | python3 -m json.tool

echo
echo "NOTE: a running client's stdio MCP keeps the catalog it started with."
echo "Restart Codex / Claude Code sessions to pick this up."
.venv/bin/python scripts/check_mcp_parity.py
