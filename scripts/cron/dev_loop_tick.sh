#!/usr/bin/env bash
# dev_loop_tick.sh — launchd-friendly wrapper that drives ONE dev-loop tick on
# the home-box Mac. launchd starts jobs with a bare environment, so (exactly like
# research-tiktok-weekly.sh) we set HOME + PATH explicitly and resolve secrets
# from a file OUTSIDE the worktree.
#
# Usage:  dev_loop_tick.sh [builder|gate|both]   (default: both)
#
# Why a wrapper instead of pointing launchd straight at the two runners:
#   1. launchd has no PATH/HOME/secrets — we inject them here, once, for both.
#   2. The dev-loop must own its OWN checkout: the builder runs
#      `git checkout -B builder/<id>`, which would hijack a shared/interactive
#      checkout. We cd to $NOVA_DEV_LOOP_REPO (default ~/.nova/loop/nova).
#   3. The /tmp/nova-dev-loop.lock flock lives only in gate_runner.sh — the
#      builder never takes it, so two independent timers could run a builder and
#      a gate at once (the OOM risk the gate's own comment warns about). Running
#      builder THEN gate in ONE wrapper, sequentially, under our own overlap
#      lock makes that overlap impossible without touching the merged runners.
#   4. ADMIN_PROD_API_KEY must NOT live in the worktree .env (the headless agent
#      runs --permission-mode bypassPermissions and could cat it; the runners
#      refuse to start via assert_no_prod_key_in_env_file if it is). We source it
#      from $NOVA_DEV_LOOP_ENV and export it so scripts/admin.py reads it from
#      os.environ (admin.py merges {**.env, **os.environ}).
#
# Manual run (bypasses the UTC work-hours guard in the runners):
#   NOVA_BUILDER_FORCE=1 bash scripts/cron/dev_loop_tick.sh builder
#   NOVA_BUILDER_FORCE=1 bash scripts/cron/dev_loop_tick.sh gate
# Before this PR is merged to main, point the wrapper at a checkout that has the
# runners (origin/main does) while running this file from the worktree:
#   NOVA_DEV_LOOP_REPO=~/.nova/loop/nova NOVA_BUILDER_FORCE=1 \
#     bash scripts/cron/dev_loop_tick.sh builder

set -uo pipefail

# ── mode (validated FIRST, before any side effects, so a typo can't no-op) ────
MODE="${1:-both}"
case "$MODE" in
  builder|gate|both) ;;
  *) echo "ERROR: unknown mode '$MODE' (expected builder|gate|both)" >&2; exit 2 ;;
esac

# ── environment (launchd provides none of this) ──────────────────────────────
export HOME="${HOME:-/Users/emirerben}"
export PATH="$HOME/.bun/bin:$HOME/.local/bin:/opt/homebrew/bin:/opt/homebrew/sbin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin"

ENV_FILE="${NOVA_DEV_LOOP_ENV:-$HOME/.nova/dev-loop.env}"
DEV_LOOP_REPO="${NOVA_DEV_LOOP_REPO:-$HOME/.nova/loop/nova}"
LOG_DIR="$HOME/.nova/logs"
mkdir -p "$LOG_DIR"
LOG="$LOG_DIR/dev-loop-$(date +%Y%m%d-%H%M%S).log"
exec >>"$LOG" 2>&1

echo "=== dev-loop tick ($MODE) start $(date) ==="

# ── secrets ──────────────────────────────────────────────────────────────────
# Only ADMIN_PROD_API_KEY is strictly required (claude + gh are already logged in
# on the box). Fail CLOSED if the env file is missing so a misconfigured box
# screams instead of silently no-op'ing every scheduled tick.
if [ ! -f "$ENV_FILE" ]; then
  echo "ERROR: secrets file $ENV_FILE not found. Run scripts/cron/install-dev-loop.sh." >&2
  exit 1
fi
# shellcheck disable=SC1090
set -a; source "$ENV_FILE"; set +a
if [ -z "${ADMIN_PROD_API_KEY:-}" ]; then
  echo "ERROR: ADMIN_PROD_API_KEY not set (looked in $ENV_FILE)." >&2
  exit 1
fi

# ── dedicated checkout (the dev-loop owns it; never the shared/interactive repo) ─
if [ ! -d "$DEV_LOOP_REPO/.git" ]; then
  echo "ERROR: dev-loop checkout $DEV_LOOP_REPO missing. Run scripts/cron/install-dev-loop.sh." >&2
  exit 1
fi
cd "$DEV_LOOP_REPO" || { echo "ERROR: cannot cd $DEV_LOOP_REPO" >&2; exit 1; }
git fetch origin main --quiet 2>/dev/null || true

# ── overlap lock ─────────────────────────────────────────────────────────────
# Keep a slow tick from stacking on the previous one. A DIFFERENT lock path from
# gate_runner.sh's /tmp/nova-dev-loop.lock so our wrapper lock and the gate's
# internal lock can never deadlock each other.
exec 8>"/tmp/nova-dev-loop-tick.lock"
if ! flock -n 8; then
  echo "[tick] a prior dev-loop tick is still running; quiet tick"
  echo "=== dev-loop tick ($MODE) end (locked) $(date) ==="
  exit 0
fi

# Run the runners as SUBPROCESSES, never `source` — each ends in `exit 0`
# (it handles its own failures via the admin API), which would kill this wrapper
# mid-tick and skip the gate.
run_builder() {
  echo "--- builder runner $(date) ---"
  bash scripts/cron/build_task_runner.sh || echo "[tick] builder runner exited $? (non-fatal)"
}
run_gate() {
  echo "--- gate runner $(date) ---"
  bash scripts/cron/gate_runner.sh || echo "[tick] gate runner exited $? (non-fatal)"
}

case "$MODE" in
  builder) run_builder ;;
  gate)    run_gate ;;
  both)    run_builder; run_gate ;;
esac

echo "=== dev-loop tick ($MODE) end $(date) ==="
exit 0
