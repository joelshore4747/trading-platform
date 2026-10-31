#!/usr/bin/env bash
# A throwaway Postgres for development and for the store tests.
#
#   scripts/dev_postgres.sh start   # start it, print the DSN
#   scripts/dev_postgres.sh stop
#   scripts/dev_postgres.sh status
#   eval "$(scripts/dev_postgres.sh env)"   # export TRADING_PLATFORM_DSN
#
# Deliberately NOT your real database. The data directory lives under /tmp and
# is expected to disappear; nothing here is a backup of anything. On a machine
# with Docker, `docker compose up -d postgres` is the same thing with a longer
# life — see docker-compose.yml.
#
# Why a script rather than "just run docker": this container has psql and the
# postgres binaries but no docker daemon, and the store tests have to be
# runnable wherever the suite runs. A test that can only run on the maintainer's
# laptop is a test that stops running.

set -euo pipefail

PGBIN="${PGBIN:-/usr/lib/postgresql/16/bin}"
PGDATA="${PGDATA:-/tmp/pgd}"
PGSOCK="${PGSOCK:-/tmp/pgs}"
PGPORT="${PGPORT:-55432}"
PGDB="${PGDB:-trading_platform}"
# The socket path goes in the DSN: a Unix socket needs no password, so nothing
# secret is printed by `env` below.
DSN="postgresql://postgres@/${PGDB}?host=${PGSOCK}&port=${PGPORT}"

die() { echo "dev_postgres: $*" >&2; exit 1; }

running() {
  # Asks the server, not the pid file. `pg_ctl status` reads $PGDATA, which
  # root cannot do once initdb has made it postgres-owned, so it reported "not
  # running" about a server that was serving queries.
  psql -h "$PGSOCK" -p "$PGPORT" -U postgres -d postgres -tAc "SELECT 1" >/dev/null 2>&1
}

as_postgres() {
  # initdb refuses to run as root, which is correct of it.
  if [ "$(id -u)" -eq 0 ]; then
    su postgres -c "$1"
  else
    bash -c "$1"
  fi
}

cmd_start() {
  [ -x "$PGBIN/initdb" ] || die "no postgres binaries at $PGBIN (set PGBIN)"

  if running; then
    echo "already running on port $PGPORT" >&2
  else
    mkdir -p "$PGDATA" "$PGSOCK"
    if [ "$(id -u)" -eq 0 ]; then
      chown -R postgres:postgres "$PGDATA" "$PGSOCK"
    fi

    if [ ! -f "$PGDATA/PG_VERSION" ]; then
      as_postgres "$PGBIN/initdb -D '$PGDATA' -U postgres --auth=trust -E UTF8" >/dev/null
    fi

    as_postgres "$PGBIN/pg_ctl -D '$PGDATA' -o '-p $PGPORT -k $PGSOCK' -l '$PGDATA/server.log' -w start" \
      >/dev/null || die "server would not start; see $PGDATA/server.log"
  fi

  # `createdb` on an existing database is an error, and not one worth reporting.
  psql -h "$PGSOCK" -p "$PGPORT" -U postgres -tAc \
    "SELECT 1 FROM pg_database WHERE datname='$PGDB'" | grep -q 1 \
    || createdb -h "$PGSOCK" -p "$PGPORT" -U postgres "$PGDB"

  echo "$DSN"
}

cmd_stop() {
  running || { echo "not running" >&2; return 0; }
  as_postgres "$PGBIN/pg_ctl -D '$PGDATA' -m fast -w stop" >/dev/null
  echo "stopped" >&2
}

cmd_status() {
  if running; then echo "running: $DSN"; else echo "not running"; exit 1; fi
}

cmd_env() {
  running || die "not running; scripts/dev_postgres.sh start"
  echo "export TRADING_PLATFORM_DSN='$DSN'"
  echo "export TRADING_PLATFORM_TEST_DSN='$DSN'"
  # If you went to the trouble of starting a database, a skipped store suite is
  # not a passing one. This turns the skip into a failure — see
  # tests/test_store_guard.py.
  echo "export TRADING_PLATFORM_STORE_TESTS_REQUIRED=1"
}

case "${1:-}" in
  start)  cmd_start ;;
  stop)   cmd_stop ;;
  status) cmd_status ;;
  env)    cmd_env ;;
  *) die "usage: $0 {start|stop|status|env}" ;;
esac
