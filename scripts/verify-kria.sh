#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
API_DIR="$ROOT_DIR/src/apps/api"
PYTHON_BIN="${KRIA_VERIFY_PYTHON:-$API_DIR/.venv/bin/python}"
RESULT_DIR="$ROOT_DIR/test-results"
RESULT_FILE="$RESULT_DIR/kria-verify.json"
START_SECONDS="$(date +%s)"
STATUS="failed"

mkdir -p "$RESULT_DIR"

finish() {
  local exit_code=$?
  local end_seconds duration
  end_seconds="$(date +%s)"
  duration=$((end_seconds - START_SECONDS))
  if [ "$exit_code" -eq 0 ]; then
    STATUS="passed"
  fi
  printf '{"schema_version":1,"status":"%s","duration_seconds":%s,"flake_retry_count":0}\n' \
    "$STATUS" "$duration" > "$RESULT_FILE"
  if [ -n "${GITHUB_STEP_SUMMARY:-}" ]; then
    printf '### Kria focused gate\n\n- Status: `%s`\n- Duration: `%ss`\n- Flake retries: `0`\n' \
      "$STATUS" "$duration" >> "$GITHUB_STEP_SUMMARY"
  fi
  exit "$exit_code"
}
trap finish EXIT

cd "$API_DIR"
"$PYTHON_BIN" -m app.cli.kria_contracts --check
"$PYTHON_BIN" -m pytest -q \
  tests/kria \
  tests/services/test_kria_editor_ops.py \
  tests/evals/test_kria_format_matrix.py \
  tests/routes/test_admin_kria.py \
  tests/routes/test_kria_runtime.py \
  tests/test_kria_runtime_v2_persistence_schema.py \
  tests/test_content_plan_schema.py::test_single_alembic_head \
  tests/test_content_plan_schema.py::test_migration_chain_is_linear \
  tests/routes/test_creation_threads.py::test_thread_creation_remains_v1_unless_client_explicitly_opts_in \
  tests/routes/test_creation_threads.py::test_runtime_v2_thread_creation_fails_closed_while_backend_flag_is_off \
  tests/routes/test_creation_threads.py::test_legacy_mutation_routes_cannot_take_authority_over_runtime_v2
