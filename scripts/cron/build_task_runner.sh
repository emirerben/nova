#!/usr/bin/env bash
# build_task_runner.sh — one timeout-bounded builder tick for the autonomous
# dev loop (M4). Invoked by .github/workflows/nova-builder.yml every ~30-60 min
# during work hours. NEVER run more than one chunk per invocation — the
# *schedule* is the auto-resume mechanism, so there is no waiting/looping logic
# here. The next scheduled run continues from the task's checkpoint.
#
# Lifecycle of one tick:
#   1. claim the oldest queued task via the admin API (SKIP LOCKED).
#   2. if the queue is empty → exit 0 (a quiet tick is healthy, not an error).
#   3. create/reuse the task's WIP branch via scripts/new-session.sh, re-orient
#      from the branch + progress_note.
#   4. run Claude Code headless on it, timeout-bounded.
#   5. on success → checkpoint (WIP commit + progress note) or complete.
#      on a usage limit / 429 → SOFT-EXIT (exit 0): PATCH action=release so the
#      task stays resumable; the next scheduled run picks it up.
#      on a genuine error → PATCH action=fail (bumps attempt_count → block@cap).
#
# Required env (set by the workflow from repo secrets):
#   ADMIN_PROD_API_KEY       — X-Admin-Token for the admin API (Fly prod).
#   CLAUDE_CODE_OAUTH_TOKEN   — Pro/Max subscription token (claude setup-token).
#   NOVA_BUILDER_RUN_ID       — opaque run identity (e.g. the GH Actions run id).
# Optional:
#   NOVA_BUILDER_TIMEOUT_S    — per-run Claude wall-clock cap (default 900 = 15m).
#   NOVA_API_BASE             — override prod base (admin.py --prod targets Fly).

set -uo pipefail

REPO_ROOT="$(git rev-parse --show-toplevel 2>/dev/null || pwd)"
cd "$REPO_ROOT" || { echo "ERROR: repo root not found"; exit 1; }

# Work-hours guard (UTC, Mon-Fri 11:00–18:59 — mirrors WORK_HOURS_UTC in
# app/tasks/send_daily_digest.py and the retired nova-builder.yml cron). The
# OpenClaw/Paperclip scheduler fires this script; the guard stops a stray
# off-hours tick from spending the shared Claude subscription. Set
# NOVA_BUILDER_FORCE=1 to bypass for a manual test tick.
if [ "${NOVA_BUILDER_FORCE:-0}" != "1" ]; then
  _H=$((10#$(date -u +%H))); _DOW=$(date -u +%u)
  if [ "$_DOW" -gt 5 ] || [ "$_H" -lt 11 ] || [ "$_H" -ge 19 ]; then
    echo "[builder] outside work-hours window (UTC Mon-Fri 11–18); quiet tick"
    exit 0
  fi
fi

ADMIN="python3 scripts/admin.py --prod --yes"
RUN_ID="${NOVA_BUILDER_RUN_ID:-local-$(date +%s)}"
TIMEOUT_S="${NOVA_BUILDER_TIMEOUT_S:-900}"

# Soft-exit helper: release the task (resumable) and exit 0 so the schedule
# resumes it. Used on any Claude usage limit / 429 — NOT a failure.
soft_exit_release() {
  local task_id="$1" note="$2"
  echo "[builder] soft-exit (limit/interrupt): releasing task $task_id for next tick"
  $ADMIN PATCH "build-tasks/$task_id" \
    --json "{\"action\": \"release\", \"progress_note\": $(json_str "$note")}" || true
  exit 0
}

# Fail helper: bump attempt_count (block at the cap), exit 0 (the run itself
# succeeded at *handling* the failure; the schedule moves on to other tasks).
fail_task() {
  local task_id="$1" note="$2"
  echo "[builder] task $task_id failed: $note"
  $ADMIN PATCH "build-tasks/$task_id" \
    --json "{\"action\": \"fail\", \"progress_note\": $(json_str "$note")}" || true
  exit 0
}

# Minimal JSON string escaper (quotes + backslashes) — avoids a jq dependency.
json_str() {
  python3 -c 'import json,sys; print(json.dumps(sys.argv[1]))' "$1"
}

# ── 1. claim ────────────────────────────────────────────────────────────────
echo "[builder] tick $RUN_ID — claiming oldest queued task"
CLAIM_JSON="$($ADMIN POST build-tasks/claim --json "{\"claimed_by\": \"$RUN_ID\"}")" || {
  echo "[builder] claim request failed (API unreachable?) — soft-exit"; exit 0
}

# Empty queue → admin returns literal `null`.
if [ -z "$CLAIM_JSON" ] || [ "$CLAIM_JSON" = "null" ]; then
  echo "[builder] queue empty — nothing to build this tick (healthy)"
  exit 0
fi

TASK_ID="$(printf '%s' "$CLAIM_JSON" | python3 -c 'import json,sys; print(json.load(sys.stdin)["id"])')"
TASK_TITLE="$(printf '%s' "$CLAIM_JSON" | python3 -c 'import json,sys; print(json.load(sys.stdin)["title"])')"
TASK_BODY="$(printf '%s' "$CLAIM_JSON" | python3 -c 'import json,sys; print(json.load(sys.stdin).get("body") or "")')"
TASK_BRANCH="$(printf '%s' "$CLAIM_JSON" | python3 -c 'import json,sys; print(json.load(sys.stdin).get("branch") or "")')"
TASK_NOTE="$(printf '%s' "$CLAIM_JSON" | python3 -c 'import json,sys; print(json.load(sys.stdin).get("progress_note") or "")')"
echo "[builder] claimed task $TASK_ID: $TASK_TITLE"

# ── 2. branch / re-orient ────────────────────────────────────────────────────
# A fresh task has no branch yet → make one off origin/main. A resumed task
# already has a WIP branch → reuse it; the prior progress note + git diff are
# the re-orientation anchor (resume is task-level, never Claude-session-level).
if [ -z "$TASK_BRANCH" ]; then
  TOPIC="builder-$(printf '%s' "$TASK_ID" | cut -c1-8)"
  git fetch origin main --quiet || true
  BRANCH="builder/${TOPIC}"
  git checkout -B "$BRANCH" origin/main || fail_task "$TASK_ID" "could not create branch $BRANCH"
  TASK_BRANCH="$BRANCH"
  $ADMIN PATCH "build-tasks/$TASK_ID" \
    --json "{\"action\": \"checkpoint\", \"branch\": $(json_str "$TASK_BRANCH"), \"stage\": \"branched\"}" || true
else
  echo "[builder] resuming on existing branch $TASK_BRANCH"
  git fetch origin "$TASK_BRANCH" --quiet 2>/dev/null || true
  git checkout "$TASK_BRANCH" 2>/dev/null || git checkout -B "$TASK_BRANCH" "origin/$TASK_BRANCH" 2>/dev/null || \
    fail_task "$TASK_ID" "could not check out resume branch $TASK_BRANCH"
fi

# ── 3. run Claude Code headless, timeout-bounded ─────────────────────────────
PROMPT="You are the Nova autonomous builder working ONE bounded chunk of a task.
Task: ${TASK_TITLE}
Details: ${TASK_BODY}
Prior progress note (if resuming): ${TASK_NOTE}

You are on git branch ${TASK_BRANCH}. Re-orient from 'git log -1' + 'git diff origin/main'.
Do a SMALL, safe chunk of work toward the task, run the relevant tests, then
WIP-commit. Do NOT deploy, merge, or touch prod. If the task is already
complete, say 'TASK COMPLETE' on its own line. Otherwise summarize what remains."

set +e
timeout "${TIMEOUT_S}s" claude --print \
  --permission-mode bypassPermissions \
  --model claude-sonnet-4-6 \
  "$PROMPT" 2>builder_stderr.log | tee builder_stdout.log
CLAUDE_EXIT=${PIPESTATUS[0]}
set -e 2>/dev/null || true

# ── 4. classify the run outcome ──────────────────────────────────────────────
# `timeout` exits 124 when it kills Claude at the wall-clock cap. A usage-limit
# / 429 surfaces as a non-zero Claude exit with a recognizable stderr string.
# Both are SOFT — the task stays resumable.
if grep -qiE 'usage limit|rate limit|429|resource_exhausted|please try again later' builder_stderr.log builder_stdout.log 2>/dev/null; then
  soft_exit_release "$TASK_ID" "paused on usage limit at stage=run ($(date -u +%FT%TZ)); resume next tick"
fi
if [ "$CLAUDE_EXIT" -eq 124 ]; then
  # Hit the per-run wall-clock cap mid-chunk — commit WIP + release (resumable).
  git add -A && git commit -m "wip(builder): timeout-bounded chunk for $TASK_ID" --no-verify || true
  git push -u origin "$TASK_BRANCH" --no-verify || true
  soft_exit_release "$TASK_ID" "hit ${TIMEOUT_S}s run cap; WIP committed, resume next tick"
fi
if [ "$CLAUDE_EXIT" -ne 0 ]; then
  fail_task "$TASK_ID" "claude exited $CLAUDE_EXIT (genuine error)"
fi

# ── 5. success: WIP commit + checkpoint, or complete ─────────────────────────
git add -A
if ! git diff --cached --quiet; then
  git commit -m "wip(builder): chunk for $TASK_ID — $TASK_TITLE" --no-verify || true
  git push -u origin "$TASK_BRANCH" --no-verify || true
fi

if grep -q 'TASK COMPLETE' builder_stdout.log 2>/dev/null; then
  echo "[builder] task $TASK_ID reported complete"
  $ADMIN PATCH "build-tasks/$TASK_ID" \
    --json "{\"action\": \"complete\", \"branch\": $(json_str "$TASK_BRANCH"), \"progress_note\": \"task complete; PR ready for evening review\"}" || true
else
  echo "[builder] task $TASK_ID checkpointed (more work remains)"
  $ADMIN PATCH "build-tasks/$TASK_ID" \
    --json "{\"action\": \"release\", \"branch\": $(json_str "$TASK_BRANCH"), \"stage\": \"chunk-done\", \"progress_note\": \"chunk committed $(date -u +%FT%TZ); resume next tick\"}" || true
fi

echo "[builder] tick $RUN_ID done"
exit 0
