#!/bin/bash
# Throwaway: shard 0 twice on one runner, shared and per-worker, order given by $1.
set -u
ORDER="$1"   # "shared per-worker" or "per-worker shared"
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
export PGPASSWORD=postgres
sql() { psql -q -h localhost -U postgres -d postgres -v ON_ERROR_STOP=1 -c "$1"; }
sql 'CREATE DATABASE nova_pristine_test TEMPLATE nova_test' || exit 2
echo "CPU: $(lscpu | sed -n 's/^Model name: *//p')"
mkdir -p "$ROOT/paired"
worst=0
for arm in $ORDER; do
  sql 'DROP DATABASE IF EXISTS nova_test WITH (FORCE)' && sql 'CREATE DATABASE nova_test TEMPLATE nova_pristine_test' || exit 2
  if [ "$arm" = per-worker ]; then export NOVA_TEST_DB_PER_WORKER=1; else export NOVA_TEST_DB_PER_WORKER=0; fi
  start=$(date +%s)
  python3 "$ROOT/scripts/ci/api-tests.py" 0 > "$ROOT/paired/$arm.log" 2>&1
  status=$?
  seconds=$(( $(date +%s) - start ))
  [ "$status" -ne 0 ] && worst=$status
  mkdir -p "$ROOT/paired/$arm/api-times-1-0-1"
  cp "$ROOT"/src/apps/api/test-results/ci-times/*.jsonl "$ROOT/paired/$arm/api-times-1-0-1/" 2>/dev/null
  cue=$(python3 - "$ROOT/paired/$arm/api-times-1-0-1" <<'PY'
import glob, json, sys
for path in glob.glob(sys.argv[1] + "/*.jsonl"):
    for line in open(path):
        row = json.loads(line)
        if row.get("kind") == "test" and row["nodeid"].endswith("styled_after_it[40]"):
            print(round(sum(row.get(p) or 0 for p in ("setup", "call", "teardown")), 1))
PY
)
  echo "PAIRED arm=$arm exit=$status step_seconds=$seconds cue40_seconds=${cue:-?} $(grep -E '^=+ .* in [0-9.]+s' "$ROOT/paired/$arm.log" | tail -1 | tr -d '=')"
  grep -E "^(FAILED|ERROR) " "$ROOT/paired/$arm.log" | head -5
done
exit "$worst"
