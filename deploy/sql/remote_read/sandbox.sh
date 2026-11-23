#!/usr/bin/env bash
# #3787: build a PRIVATE, throwaway PostgreSQL cluster to test the remote_read
# role and views against the real table shapes, without touching production.
#
#   deploy/sql/remote_read/sandbox.sh <empty-dir>
#
# - Production is only READ, schema-only (pg_dump -s, no rows) for exactly the
#   base tables the views name.
# - The sandbox listens on a unix socket inside <dir> only (listen_addresses='').
# - Prints the env line the tests need, then leaves the cluster running;
#   stop it with: pg_ctl -D <dir>/data stop   (then rm -rf <dir> and the socket dir)
set -euo pipefail
DIR=${1:?usage: sandbox.sh <empty-dir>}
PGBIN=${PGBIN:-/usr/lib/postgresql/16/bin}
HERE=$(cd "$(dirname "$0")" && pwd)
PORT=${SANDBOX_PORT:-55439}
SRC_HOST=${SRC_HOST:-localhost}
mkdir -p "$DIR"
[ -z "$(ls -A "$DIR")" ] || { echo "refusing: $DIR is not empty" >&2; exit 2; }
DIR=$(cd "$DIR" && pwd)
# Unix socket paths are limited to 107 bytes: keep the socket in a short,
# private (0700) directory of its own.
SOCK=$(mktemp -d /tmp/rr-sock.XXXXXX)

"$PGBIN/initdb" -D "$DIR/data" -U sandbox_super --auth=trust >/dev/null
cat >>"$DIR/data/postgresql.conf" <<EOF
listen_addresses = ''
unix_socket_directories = '$SOCK'
port = $PORT
fsync = off
EOF
"$PGBIN/pg_ctl" -D "$DIR/data" -l "$DIR/pg.log" -w start >/dev/null

SB=(psql -h "$SOCK" -p "$PORT" -U sandbox_super -X -q)
dump_tables() {  # db table...
  local db=$1; shift
  local args=()
  for t in "$@"; do args+=(-t "public.$t"); done
  # schema only, no owners/grants; triggers/defaults whose functions are not
  # dumped fail harmlessly below -- the tables (what the views read) load.
  pg_dump -h "$SRC_HOST" -U "${SRC_USER:-patrick}" -d "$db" -s --no-owner --no-privileges "${args[@]}"
}

"${SB[@]}" -d postgres -c "CREATE DATABASE echo_brain" -c "CREATE DATABASE anime_production"
"${SB[@]}" -d postgres -v ON_ERROR_STOP=1 -f "$HERE/00_roles.sql"
"${SB[@]}" -d postgres -c "ALTER ROLE tower_remote_ro PASSWORD 'sandbox-only'"

dump_tables echo_brain tower_tasks tower_projects tower_task_runs operator_questions \
  notifications echo_telegram_messages telegram_inbox \
  | "${SB[@]}" -d echo_brain >/dev/null 2>"$DIR/echo_schema.err" || true
dump_tables anime_production production_assignments production_assignment_items \
  production_assignment_submissions production_assignment_stop_choices dispatch_holds \
  shots scenes projects gpu_claims render_admission_executions \
  render_admission_execution_refusals video_submissions \
  | "${SB[@]}" -d anime_production >/dev/null 2>"$DIR/anime_schema.err" || true

"${SB[@]}" -d echo_brain -v ON_ERROR_STOP=1 -f "$HERE/echo_brain.sql"
"${SB[@]}" -d anime_production -v ON_ERROR_STOP=1 -f "$HERE/anime_production.sql"

echo "REMOTE_READ_SANDBOX_HOST=$SOCK REMOTE_READ_SANDBOX_PORT=$PORT"
