#!/usr/bin/env bash
# _dev_loop_lib.sh — shared helpers for the dev-loop builder + gate ticks.
#
# Source this AFTER defining $ADMIN (the scripts/admin.py invocation). The point
# of the lib is a SINGLE canonical copy of the security scan + the .env guard, so
# a fix can never drift between build_task_runner.sh and gate_runner.sh.
#
# All functions assume the cwd is the repo root and $ADMIN is set.

# Refuse to run if the prod admin key is readable from the worktree .env — the
# headless agent runs with --permission-mode bypassPermissions and could cat it.
# The home-box scheduler must export ADMIN_PROD_API_KEY as an env var instead.
assert_no_prod_key_in_env_file() {
  if [ -f .env ] && grep -qE '^[[:space:]]*ADMIN_PROD_API_KEY[[:space:]]*=' .env; then
    echo "ERROR: ADMIN_PROD_API_KEY found in worktree .env — the headless agent could read it." >&2
    echo "  Remove it from .env and set it as an env var in the scheduler job instead." >&2
    exit 1
  fi
}

# Work-hours guard (UTC Mon-Fri 11:00–18:59). NOVA_BUILDER_FORCE=1 bypasses for a
# manual tick. Exits 0 (a quiet off-hours tick is healthy, not an error).
work_hours_guard_or_exit() {
  [ "${NOVA_BUILDER_FORCE:-0}" = "1" ] && return 0
  local h dow
  h=$((10#$(date -u +%H)))
  dow=$(date -u +%u)
  if [ "$dow" -gt 5 ] || [ "$h" -lt 11 ] || [ "$h" -ge 19 ]; then
    echo "[dev-loop] outside work-hours window (UTC Mon-Fri 11–18); quiet tick"
    exit 0
  fi
}

# Minimal JSON string escaper (quotes + backslashes) — avoids a jq dependency.
json_str() {
  python3 -c 'import json,sys; print(json.dumps(sys.argv[1]))' "$1"
}

# Portable advisory lock. flock is util-linux and ABSENT on stock macOS (where
# `if ! flock -n` inverts to "held" and the tick silently no-ops every run), so
# use an atomic `mkdir`: the first caller creates the dir; a second caller's
# mkdir fails. A lock left by a crashed tick is reclaimed once it is older than
# DEV_LOOP_LOCK_STALE_S (default 2h — safely longer than any real tick, so a live
# long gate is never yanked) so a hard-killed run can't wedge the loop forever.
# On success it arms an EXIT trap to self-release (do NOT combine with another
# EXIT trap in the same script). Returns 0 if acquired, 1 if another live tick
# holds it.
DEV_LOOP_LOCK_STALE_S="${DEV_LOOP_LOCK_STALE_S:-7200}"
acquire_lock() {
  local dir="$1"
  if mkdir "$dir" 2>/dev/null; then
    echo "$$" > "$dir/pid" 2>/dev/null || true
    # shellcheck disable=SC2064
    trap "release_lock '$dir'" EXIT
    return 0
  fi
  # Lock dir exists — reclaim only if stale (crashed holder). stat is BSD on
  # macOS (-f %m) and GNU on Linux (-c %Y); try both.
  local now mtime age
  now="$(date +%s)"
  mtime="$(stat -f %m "$dir" 2>/dev/null || stat -c %Y "$dir" 2>/dev/null || echo "$now")"
  age=$(( now - mtime ))
  if [ "$age" -ge "$DEV_LOOP_LOCK_STALE_S" ]; then
    echo "[dev-loop] reclaiming stale lock $dir (age ${age}s)" >&2
    rm -rf "$dir"
    if mkdir "$dir" 2>/dev/null; then
      echo "$$" > "$dir/pid" 2>/dev/null || true
      # shellcheck disable=SC2064
      trap "release_lock '$dir'" EXIT
      return 0
    fi
  fi
  return 1
}

release_lock() {
  local dir="$1"
  [ -n "$dir" ] && [ -d "$dir" ] && rm -rf "$dir"
}

# Hard-block: a secret is about to leak — block the task for human review
# (action=block, no retry) instead of pushing it. Exits 1 (genuine abort).
abort_block() {
  local task_id="$1" note="$2"
  echo "[dev-loop][SECURITY] $note" >&2
  $ADMIN PATCH "build-tasks/$task_id" \
    --json "{\"action\": \"block\", \"progress_note\": $(json_str "$note")}" || true
  exit 1
}

# Scan the diff about to be pushed for secrets, BEFORE the push. Runs
# unconditionally (the agent runs with bypassPermissions, so this is the last
# line of defense before bytes hit origin). --no-verify on the push can't bypass
# it — it's an explicit call, not a git hook. gitleaks if present + always a scan
# for the live credential VALUES + a few high-signal patterns.
secret_scan_or_abort() {
  local task_id="$1"
  git fetch origin main --quiet 2>/dev/null || true
  local diff
  diff="$(git diff origin/main...HEAD 2>/dev/null)"
  [ -z "$diff" ] && return 0
  if command -v gitleaks >/dev/null 2>&1; then
    gitleaks detect --no-banner --redact --log-opts='origin/main...HEAD' >/dev/null 2>&1 ||
      abort_block "$task_id" "ABORTED: gitleaks flagged a secret in the outgoing diff; not pushed. Manual review needed."
  fi
  local v
  for v in "${ADMIN_PROD_API_KEY:-}" "${ADMIN_API_KEY:-}" "${CLAUDE_CODE_OAUTH_TOKEN:-}" "${GH_TOKEN:-}"; do
    if [ -n "$v" ] && printf '%s' "$diff" | grep -qF -- "$v"; then
      abort_block "$task_id" "ABORTED: a live credential value appeared in the outgoing diff; not pushed. Manual review needed."
    fi
  done
  if printf '%s' "$diff" | grep -qiE 'sk-[a-z0-9]{20,}|AKIA[0-9A-Z]{16}|-----BEGIN ([A-Z]+ )?PRIVATE KEY-----|xox[baprs]-[0-9A-Za-z-]{10,}'; then
    abort_block "$task_id" "ABORTED: a secret-like pattern appeared in the outgoing diff; not pushed. Manual review needed."
  fi
  return 0
}

# macOS python.org Python ships WITHOUT a CA bundle, so admin.py's urllib TLS
# call to prod fails with CERTIFICATE_VERIFY_FAILED — which would make every tick
# silently unable to reach the queue API on the home box. Point urllib at
# certifi's bundle if it's present and SSL_CERT_FILE isn't already set. Guarded
# so we never export an empty value (which would itself break verification).
if [ -z "${SSL_CERT_FILE:-}" ]; then
  _certifi_path="$(python3 -c 'import certifi; print(certifi.where())' 2>/dev/null || true)"
  [ -n "$_certifi_path" ] && export SSL_CERT_FILE="$_certifi_path"
  unset _certifi_path
fi
