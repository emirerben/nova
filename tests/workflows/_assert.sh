#!/usr/bin/env bash
#
# Tiny assert helpers for the Lane C workflow tests. Kept in-repo (not pulled
# from a test framework) because the workflows are shell + YAML and the only
# realistic harness is shell. Sourced by the per-workflow test scripts.

set -uo pipefail

# Per-test counters; reset by `reset_counters` between suites if needed.
PASS_COUNT=${PASS_COUNT:-0}
FAIL_COUNT=${FAIL_COUNT:-0}
CASE_NAME=${CASE_NAME:-""}

start_case() {
  CASE_NAME="$1"
}

pass() {
  PASS_COUNT=$((PASS_COUNT + 1))
  printf '  PASS  %s\n' "$CASE_NAME"
}

fail() {
  FAIL_COUNT=$((FAIL_COUNT + 1))
  printf '  FAIL  %s — %s\n' "$CASE_NAME" "$1" >&2
}

assert_eq() {
  local expected="$1"
  local actual="$2"
  if [[ "$expected" == "$actual" ]]; then
    pass
  else
    fail "expected=<$expected> actual=<$actual>"
  fi
}

summary() {
  local total=$((PASS_COUNT + FAIL_COUNT))
  echo
  echo "Result: $PASS_COUNT/$total passing ($FAIL_COUNT failed)"
  if [[ $FAIL_COUNT -gt 0 ]]; then
    return 1
  fi
  return 0
}
