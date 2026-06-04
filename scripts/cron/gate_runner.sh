#!/usr/bin/env bash
# gate_runner.sh — one GATE tick for the dev-loop ship-gate (Phase 2).
#
# Claims a built task the builder left in `gating`, asserts it is gating the
# exact pushed commit, rebases the branch onto current origin/main (so "green"
# means green against the main you'll actually merge into), runs the HARD gates,
# and on green opens a PR + parks the task in `awaiting_approval` for the founder
# to merge by hand. On a blocking-gate failure it records the report and routes
# the task back for another builder chunk.
#
# A SEPARATE tick from the builder on purpose: the gate run (full test matrix +
# a Dockerized verify-overlays) needs its own, longer timeout and different
# failure semantics (a gate-tick TIMEOUT is an abort — release, no attempt bump;
# a real gate FAILURE bumps). Folding it into the builder's 900s tick would
# reintroduce the stale-base + timeout-conflation bugs this split exists to kill.
#
# Required env: ADMIN_PROD_API_KEY (queue API), GH_TOKEN (gh pr create).
# Optional: NOVA_GATE_TIMEOUT_S (default 2400 — a cold Docker verify-overlays +
# full tests), NOVA_BUILDER_FORCE=1 to bypass the work-hours guard.

set -uo pipefail

REPO_ROOT="$(git rev-parse --show-toplevel 2>/dev/null || pwd)"
cd "$REPO_ROOT" || { echo "ERROR: repo root not found"; exit 1; }

# shellcheck source=scripts/cron/_dev_loop_lib.sh
source "scripts/cron/_dev_loop_lib.sh"

ADMIN="python3 scripts/admin.py --prod --yes"
RUN_ID="${NOVA_GATE_RUN_ID:-gate-$(date +%s)}"

assert_no_prod_key_in_env_file
work_hours_guard_or_exit

# Global mutex: build + gate ticks must NEVER run concurrently on this host — two
# full test/Docker runs at once OOMs the daily-driver Mac (same class as the
# logged Fly OOM), and an OOM-killed gate reads as a false failure. flock is
# util-linux (absent on stock macOS), so use the lib's portable mkdir lock.
if ! acquire_lock "/tmp/nova-dev-loop.lock.d"; then
  echo "[gate] another dev-loop tick holds the lock; quiet tick"
  exit 0
fi

# Gate-tick ABORT: release the task back to queued WITHOUT a bump (a timeout /
# infra fault is not the code's fault — distinct from gate_failed, which bumps).
gate_release() {
  local task_id="$1" note="$2"
  echo "[gate] releasing $task_id: $note"
  $ADMIN PATCH "build-tasks/$task_id" \
    --json "{\"action\": \"release\", \"progress_note\": $(json_str "$note")}" || true
  exit 0
}

# ── claim a gating task ─────────────────────────────────────────────────────
CLAIM_JSON="$($ADMIN POST build-tasks/claim-gating --json "{\"claimed_by\": \"$RUN_ID\"}")" || {
  echo "[gate] claim-gating request failed (API unreachable?)"; exit 0
}
if [ -z "$CLAIM_JSON" ] || [ "$CLAIM_JSON" = "null" ]; then
  echo "[gate] no gating tasks — quiet tick"; exit 0
fi

_get() { printf '%s' "$CLAIM_JSON" | python3 -c "import json,sys;print(json.load(sys.stdin).get('$1') or '')"; }
TASK_ID="$(_get id)"
TASK_TITLE="$(_get title)"
BRANCH="$(_get branch)"
HEAD_SHA="$(_get head_sha)"
echo "[gate] claimed $TASK_ID ($TASK_TITLE) on $BRANCH @ ${HEAD_SHA:0:12}"

if [ -z "$BRANCH" ] || [ -z "$HEAD_SHA" ]; then
  gate_release "$TASK_ID" "gating task missing branch/head_sha; releasing"
fi

# ── head_sha assert (never gate a branch the builder never finished pushing) ──
git fetch origin "$BRANCH" --quiet 2>/dev/null || gate_release "$TASK_ID" "could not fetch $BRANCH"
ORIGIN_HEAD="$(git rev-parse "origin/$BRANCH" 2>/dev/null || echo "")"
if [ "$ORIGIN_HEAD" != "$HEAD_SHA" ]; then
  gate_release "$TASK_ID" "origin/$BRANCH ($ORIGIN_HEAD) != head_sha ($HEAD_SHA); stale/partial push, re-gate next tick"
fi
git checkout -B "$BRANCH" "origin/$BRANCH" --quiet || gate_release "$TASK_ID" "checkout $BRANCH failed"

# ── rebase onto current main (green must mean green vs the main we'll merge) ──
git fetch origin main --quiet || true
BASE_SHA="$(git rev-parse origin/main 2>/dev/null || echo "")"
if ! git merge --no-commit --no-ff origin/main >/dev/null 2>&1; then
  git merge --abort 2>/dev/null || true
  # A conflict with main is drift needing a builder chunk to resolve → back to
  # queued (no bump — not the code being bad, just diverged).
  $ADMIN PATCH "build-tasks/$TASK_ID" \
    --json "{\"action\": \"release\", \"progress_note\": $(json_str "rebase conflict with origin/main; needs a builder chunk to resolve")}" || true
  echo "[gate] rebase conflict; released $TASK_ID"; exit 0
fi
# The merged tree is now staged (uncommitted) — gates run against the would-be-
# merged code, which is exactly what a human merge would produce.

# ── run the hard gates, collecting results into a JSON array ───────────────────
RESULTS="$(mktemp)"; printf '[]' > "$RESULTS"
add_result() { # name blocking(0/1) passed(0/1) detail
  python3 - "$RESULTS" "$1" "$2" "$3" "$4" <<'PY'
import json, sys
path, name, blocking, passed, detail = sys.argv[1:6]
data = json.load(open(path))
data.append({"name": name, "blocking": blocking == "1", "passed": passed == "1", "detail": detail})
json.dump(data, open(path, "w"))
PY
}
run_gate() { # name blocking(0/1) -- cmd...
  local name="$1" blocking="$2"; shift 2
  if timeout "${NOVA_GATE_TIMEOUT_S:-2400}s" "$@" >"/tmp/gate-$name.log" 2>&1; then
    add_result "$name" "$blocking" 1 ""
  else
    local rc=$?
    # A gate-tick timeout (124) is an ABORT, not a failure — release, no bump.
    [ "$rc" -eq 124 ] && gate_release "$TASK_ID" "gate '$name' hit the ${NOVA_GATE_TIMEOUT_S:-2400}s cap; re-gate next tick"
    add_result "$name" "$blocking" 0 "exit $rc; see /tmp/gate-$name.log"
  fi
}

# Mirror CI's exact pytest invocation (.github/workflows/ci.yml test-api) so the
# gate predicts CI: ignore the non-test helper dir, run in parallel, cap per-test.
run_gate pytest 1 bash -c "cd src/apps/api && python -m pytest tests/ --ignore=tests/quality -n auto --timeout=60 -q"
run_gate ruff 1 bash -c "cd src/apps/api && ruff check ."
run_gate npm-test 1 bash -c "cd src/apps/web && npm test --silent"
# NOTE: no `tsc --noEmit` gate — CI does not run it and the project does not pass
# a bare `npx tsc --noEmit` (the __tests__ files reference jest globals the root
# tsconfig doesn't type). Gating on it would block every task. Re-add here only
# once CI enforces it.
run_gate lint 1 bash -c "cd src/apps/web && npm run lint --silent"

# verify-overlays is BLOCKING but only runs when render paths changed (the
# detector lives in tested Python; default-ON, the #296 guard).
if git diff origin/main...HEAD | python3 -m app.cli.gate render-needed; then
  run_gate verify-overlays 1 make verify-overlays
else
  echo "[gate] no render-path change; skipping verify-overlays"
fi

# Advisory (non-blocking) — reported in the PR body, never gates (the /qa-flaky
# decision). /qa headless + codex wiring is a follow-up; recorded as advisory.
add_result qa 0 1 "advisory /qa not yet wired headless"

# ── decide: open PR (green) or gate_failed (blocking red) ─────────────────────
PR_BODY="$(mktemp)"
GATE_REPORT="$(python3 -m app.cli.gate report --task-id "$TASK_ID" --task-title "$TASK_TITLE" --head "$HEAD_SHA" --base "$BASE_SHA" --pr-body-out "$PR_BODY" < "$RESULTS")"
PASSED="$(printf '%s' "$GATE_REPORT" | python3 -c 'import json,sys;print("1" if json.load(sys.stdin)["passed"] else "0")')"

if [ "$PASSED" = "1" ]; then
  # Commit the merge (if origin/main moved) + push, AFTER the secret scan.
  git commit -m "gate: merge origin/main into $BRANCH" --no-verify >/dev/null 2>&1 || true
  secret_scan_or_abort "$TASK_ID"
  git push -u origin "$BRANCH" --no-verify --force-with-lease || gate_release "$TASK_ID" "push failed"
  gh pr create --title "$TASK_TITLE" --body-file "$PR_BODY" --base main --head "$BRANCH" >/dev/null 2>&1 || true
  PR_URL="$(gh pr view "$BRANCH" --json url -q .url 2>/dev/null || echo "")"
  PR_NUM="$(gh pr view "$BRANCH" --json number -q .number 2>/dev/null || echo "")"
  $ADMIN PATCH "build-tasks/$TASK_ID" --json "$(python3 - "$PR_URL" "$PR_NUM" "$GATE_REPORT" <<'PY'
import json, sys
url, num, report = sys.argv[1], sys.argv[2], sys.argv[3]
payload = {"action": "open_pr", "pr_url": url or "(pr create returned no url)", "gate_report": json.loads(report)}
if num.isdigit():
    payload["pr_number"] = int(num)
print(json.dumps(payload))
PY
)" || true
  echo "[gate] PR opened for $TASK_ID: ${PR_URL:-<none>}"
else
  git merge --abort 2>/dev/null || true
  $ADMIN PATCH "build-tasks/$TASK_ID" --json "$(python3 - "$GATE_REPORT" <<'PY'
import json, sys
report = json.loads(sys.argv[1])
fails = [r["name"] for r in report["results"] if r["blocking"] and not r["passed"]]
print(json.dumps({
    "action": "gate_failed",
    "gate_report": report,
    "progress_note": "blocking gate(s) failed: " + ", ".join(fails) + "; needs another builder chunk",
}))
PY
)" || true
  echo "[gate] gate failed for $TASK_ID"
fi
exit 0
