#!/usr/bin/env bash
set -euo pipefail

repo_dir=$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)
test_python=${FUJU_SQL_PYTHON:-python3}

for program in initdb pg_ctl createdb; do
  if ! command -v "$program" >/dev/null 2>&1; then
    printf 'missing PostgreSQL command: %s\n' "$program" >&2
    exit 1
  fi
done
"$test_python" -c 'import psycopg2' || {
  printf 'install fuju-trace-sql[postgresql] for %s\n' "$test_python" >&2
  exit 1
}

pg_test_dir=$(mktemp -d /tmp/fuju-trace-pg.XXXXXX)
cleanup() {
  pg_ctl -D "$pg_test_dir/data" -m immediate stop >/dev/null 2>&1 || true
  rm -r -- "$pg_test_dir"
}
trap cleanup EXIT

initdb -D "$pg_test_dir/data" -A trust -U fuju_test --no-instructions > "$pg_test_dir/init.log"
pg_ctl -D "$pg_test_dir/data" -l "$pg_test_dir/server.log" \
  -o "-c listen_addresses='' -c unix_socket_directories=$pg_test_dir -p 55432" start
createdb -h "$pg_test_dir" -p 55432 -U fuju_test fuju_test

POSTGRESQL_DSN="host=$pg_test_dir port=55432 dbname=fuju_test user=fuju_test" \
  "$test_python" "$repo_dir/fuju-trace-sql/tests/live_smoke.py"
