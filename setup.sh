#!/usr/bin/env bash
# ai-team-sync: one command, done.
set -e

DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$DIR"

echo "ai-team-sync — installing..."

# Python backend
if [ ! -d ".venv" ]; then
    python3 -m venv .venv
fi
.venv/bin/pip install -e . -q 2>&1 | tail -1

# Config
[ ! -f ".env" ] && cp .env.example .env

# VS Code extension
cd "$DIR/vscode-extension"
if [ ! -d "node_modules" ]; then
    npm install --silent 2>/dev/null
fi
npm run compile --silent 2>/dev/null
npm run package --silent 2>/dev/null

# Install into VS Code (try both code and codium)
VSIX="$DIR/vscode-extension/ai-team-sync-0.2.0.vsix"
for cmd in code codium code-insiders; do
    if command -v "$cmd" &>/dev/null; then
        "$cmd" --install-extension "$VSIX" --force 2>/dev/null && echo "  VS Code extension installed ($cmd)" && break
    fi
done

# Start server. If the systemd user unit is installed it OWNS the server and its
# database path — killing :8400 and relaunching here would swap the live database
# for whatever a relative sqlite URL resolves to under this CWD.
if systemctl --user list-unit-files ats-server.service &>/dev/null \
   && systemctl --user is-enabled ats-server &>/dev/null; then
    systemctl --user restart ats-server
else
    fuser -k 8400/tcp 2>/dev/null || true
    sleep 1
    cd "$DIR"
    ATS_HOST="${ATS_HOST:-127.0.0.1}"
    nohup .venv/bin/uvicorn ai_team_sync.server:app --host "$ATS_HOST" --port 8400 &>/tmp/ats-server.log &
fi
sleep 2

# Get IP for optional trusted-network access
IP=$(hostname -I 2>/dev/null | awk '{print $1}')

# Verify
if curl -s http://localhost:8400/health | grep -q ok; then
    echo ""
    echo "  Ready."
    echo ""
    echo "  VS Code:  Ctrl+Shift+P → 'AI Team Sync: Run Demo' to see how it works"
    echo "            Ctrl+Shift+P → 'AI Team Sync: Start Session' to begin"
    echo ""
    echo "  Browser:  http://localhost:8400/dashboard"
    if [ "$ATS_HOST" = "0.0.0.0" ] && [ -n "$IP" ]; then
        echo "  Remote:   http://${IP}:8400/dashboard"
    fi
    echo ""
else
    echo "  Server failed to start. Check /tmp/ats-server.log"
fi
