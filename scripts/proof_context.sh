#!/usr/bin/env bash
# proof_context.sh — capture WHICH CODE a live proof actually exercised.
#
# WHY THIS EXISTS
# ---------------
# Twice in the A-D tranches a "live proof" ran against code that was not the
# code under test, and the harness said nothing:
#
#   1. `pipx install --force` skipped the build stamp, so /api/version kept
#      reporting commit 0b36b92 while f1a306b was running. The deployment gate
#      passed on a build nobody had verified.
#   2. A stdio MCP server is spawned ONCE per agent session and keeps that build
#      for the session's whole life. Completing a session from the already-open
#      client exercised the PRE-tranche-D handler and printed the old payload,
#      after the new one had been deployed and pushed.
#
# In both cases the system behaved correctly. The TEST HARNESS lied about what
# had been exercised, which is worse than a failing test: it produces confident
# evidence for an untested path.
#
# So a live proof now states its context before it claims anything. Run this,
# paste or attach the output, and the claim carries the code it was made
# against.
#
# USAGE
#   scripts/proof_context.sh                 # human-readable
#   scripts/proof_context.sh --json          # machine-readable
#
# READ-ONLY. Starts nothing, restarts nothing, mutates nothing.

set -uo pipefail

ATS_REPO="${ATS_REPO:-/home/patrick/code/ai-team-sync}"
ECHO_REPO="${ECHO_REPO:-/opt/tower-echo-brain}"
ATS_URL="${ATS_SERVER_URL:-http://localhost:8400}"
ECHO_URL="${ECHO_BRAIN_URL:-http://localhost:8309}"

_head()  { git -C "$1" rev-parse --short HEAD 2>/dev/null || echo "unknown"; }
_dirty() { [ -n "$(git -C "$1" status --porcelain 2>/dev/null | grep -v '\.worktrees')" ] \
             && echo true || echo false; }
_sync()  { git -C "$1" status -sb 2>/dev/null | head -1 | grep -oE '\[(ahead|behind)[^]]*\]' \
             || echo "[in sync]"; }

_json_field() {  # $1=url $2=key
  curl -s --max-time 5 "$1" 2>/dev/null \
    | python3 -c "import json,sys;print(json.load(sys.stdin).get('$2','unknown'))" 2>/dev/null \
    || echo "unreachable"
}

ATS_HEAD="$(_head "$ATS_REPO")"
ECHO_HEAD="$(_head "$ECHO_REPO")"
ATS_DEPLOYED="$(_json_field "$ATS_URL/api/version" commit)"
ATS_PID="$(_json_field "$ATS_URL/api/version" pid)"
ATS_STARTED="$(_json_field "$ATS_URL/api/version" process_started_at)"

# The Echo Brain service has no /api/version, so its deployed revision is the
# repo HEAD its running process was started from. Report the process start so a
# reader can compare it against the commit time themselves rather than trusting
# an inference.
ECHO_PID="$(systemctl show tower-echo-brain -p MainPID --value 2>/dev/null || echo 0)"
ECHO_STARTED="$(ps -o lstart= -p "${ECHO_PID:-0}" 2>/dev/null | sed 's/^ *//' || echo unknown)"
ECHO_COMMIT_TIME="$(git -C "$ECHO_REPO" log -1 --format=%ci 2>/dev/null || echo unknown)"

# THE ONE THAT BIT US TWICE. An MCP server started before a deploy holds the old
# build for its whole life, so a proof from an already-open client proves
# nothing about what was just shipped.
MCP_PIDS="$(pgrep -f 'bin/ats-mcp' 2>/dev/null | tr '\n' ' ')"

if [ "${1:-}" = "--json" ]; then
  python3 - "$ATS_HEAD" "$(_dirty "$ATS_REPO")" "$ATS_DEPLOYED" "$ATS_PID" "$ATS_STARTED" \
              "$ECHO_HEAD" "$(_dirty "$ECHO_REPO")" "$ECHO_PID" "$ECHO_STARTED" \
              "$ECHO_COMMIT_TIME" "$MCP_PIDS" <<'PY'
import json, sys
k = ["ats_head","ats_dirty","ats_deployed","ats_pid","ats_started",
     "echo_head","echo_dirty","echo_pid","echo_started","echo_last_commit",
     "ats_mcp_pids"]
d = dict(zip(k, sys.argv[1:]))
d["ats_deploy_matches_head"] = d["ats_deployed"].startswith(d["ats_head"])
print(json.dumps(d, indent=2))
PY
  exit 0
fi

echo "PROOF CONTEXT — what code this proof actually exercises"
echo "  captured: $(date -Is)"
echo
printf "  %-22s %s\n" "ai-team-sync HEAD"   "$ATS_HEAD (dirty=$(_dirty "$ATS_REPO")) $(_sync "$ATS_REPO")"
printf "  %-22s %s\n" "ATS deployed"        "$ATS_DEPLOYED  pid $ATS_PID  up $ATS_STARTED"
if [ "$ATS_DEPLOYED" = "unreachable" ]; then
  echo "    !! ATS did not answer. A proof about ATS behaviour cannot be made now."
elif [ "${ATS_DEPLOYED#$ATS_HEAD}" = "$ATS_DEPLOYED" ]; then
  echo "    !! DEPLOYED != HEAD. Run scripts/deploy.sh; a bare pipx install skips the stamp."
fi
echo
printf "  %-22s %s\n" "tower-echo-brain HEAD" "$ECHO_HEAD (dirty=$(_dirty "$ECHO_REPO")) $(_sync "$ECHO_REPO")"
printf "  %-22s %s\n" "  last commit at"      "$ECHO_COMMIT_TIME"
printf "  %-22s %s\n" "  service pid"         "${ECHO_PID:-none}"
printf "  %-22s %s\n" "  process started"     "$ECHO_STARTED"
if [ "$ECHO_STARTED" != "unknown" ]; then
  echo "    NOTE: a restart deploys whatever was ON DISK at that moment, committed or"
  echo "    not. So a process older than the last commit is AMBIGUOUS, not proof of"
  echo "    staleness: it may have been restarted from a dirty tree carrying that same"
  echo "    code. Settle it behaviourally (call the changed path), never by timestamps."
fi
echo
echo "  ats-mcp processes: ${MCP_PIDS:-none}"
for pid in $MCP_PIDS; do
  started="$(ps -o lstart= -p "$pid" 2>/dev/null | sed 's/^ *//')"
  printf "    pid %-8s started %s\n" "$pid" "${started:-unknown}"
done
echo
echo "  RULE: a stdio MCP server keeps its SPAWN-TIME build for the session's whole"
echo "  life. Any client above that started BEFORE the deploy proves nothing about"
echo "  the new code. Spawn a fresh client for the proof, or state plainly which"
echo "  build the claim was made against."
