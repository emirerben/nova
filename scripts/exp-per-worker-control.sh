#!/bin/bash
# Throwaway red control: the notification file with its lock removed, four workers,
# on one shared database and on one database per worker. Fresh database every try.
set -u
TRIES="${1:-12}"
cd "$(dirname "$0")/../src/apps/api"
export PGPASSWORD=postgres
sql() { psql -q -h localhost -U postgres -d postgres -v ON_ERROR_STOP=1 -c "$1"; }
sql 'CREATE DATABASE nova_pristine_test TEMPLATE nova_test' || exit 2
export EXP_NO_NOTIFICATION_LOCK=1 NOVA_REQUIRE_POSTGRES_TESTS=1
mkdir -p exp-control
i=1
while [ "$i" -le "$TRIES" ]; do
  for arm in shared per-worker; do
    sql 'DROP DATABASE IF EXISTS nova_test WITH (FORCE)' && sql 'CREATE DATABASE nova_test TEMPLATE nova_pristine_test' || exit 2
    if [ "$arm" = per-worker ]; then
      python -m tests._worker_database 4 >/dev/null || exit 2
      export NOVA_TEST_DB_WORKER_COPIES_OF="$DATABASE_URL"
    else
      unset NOVA_TEST_DB_WORKER_COPIES_OF
    fi
    log="exp-control/$arm-$i.log"
    pytest tests/routes/test_notification_delivery_postgres.py -n 4 -q -p no:cacheprovider --timeout=60 > "$log" 2>&1
    status=$?
    echo "[$arm $i] exit $status; $(tail -1 "$log")"
  done
  i=$((i + 1))
done
echo "== summary"
for arm in shared per-worker; do
  total=$(ls exp-control/$arm-*.log | wc -l | tr -d ' ')
  red=$(grep -lE "[0-9]+ (failed|error)" exp-control/$arm-*.log | wc -l | tr -d ' ')
  echo "$arm: $red of $total tries had a failure"
done
