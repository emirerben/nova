#!/usr/bin/env bash
#
# fly-deploy-with-retry.sh — wrap `flyctl deploy` with a single, signature-
# gated retry for the intermittent release-machine startup kill (issue #834).
#
# Context:
#   `Fly Deploy` occasionally goes red with exit 143 (SIGTERM) on the
#   release_command machine (`python -m alembic upgrade head`) while it is
#   still starting up — the machine is destroyed ~3s in, well before alembic
#   could plausibly have failed, and the very next deploy of the SAME code
#   succeeds. This has been root-caused as a Fly platform-side machine
#   lifecycle race, NOT `REMAP_SIGTERM` leaking from fly.toml [env] onto the
#   release machine: that var is read only by the Celery worker process
#   itself (a Celery 5.5 soft-shutdown feature), never by Fly's init, and is
#   inert on the release machine because `python -m alembic` never reads it.
#   Proof: exit 143 = 128+15 = plain SIGTERM; a SIGQUIT death (what a remap
#   would actually cause) would be 131. See agents/DECISIONS.md
#   (2026-08-17) for the full writeup.
#
#   This script retries ONLY that exact signature — BOTH "has state:
#   destroyed" AND "release_command failed ... with exit code 143" present
#   in the deploy log — exactly once. Any other failure (e.g. a genuine
#   `raise` inside a migration, which exits 1) fails immediately on the
#   first attempt, visibly. It can never mask a real regression the way the
#   #812 24-hour deploy freeze was hidden by earlier noise on this pipeline.
#
# Exit codes:
#   0    — flyctl deploy succeeded (first attempt, or after one
#          signature-gated retry)
#   *    — whatever flyctl itself returned, unmodified: on a non-signature
#          failure this is the first attempt's code (e.g. 1 for a migration
#          error); on a signature match that also fails on retry, this is
#          the second attempt's code (typically 1 — flyctl reports the
#          release command's 143 in its log but exits 1 itself)
#
# Env:
#   FLY_DEPLOY_RETRY_DELAY_SECONDS — seconds to sleep before the retry
#                                    (default 5; tests set 0)
#   GITHUB_STEP_SUMMARY            — if set, a retry appends a summary block
#                                    with a 40-line log tail (CI sets this
#                                    automatically; unset locally is fine —
#                                    no crash)
#
# Usage:
#   scripts/fly-deploy-with-retry.sh --remote-only
#   (all args are forwarded verbatim to `flyctl deploy`)

set -uo pipefail

work_dir="$(mktemp -d)"
trap 'rm -rf "$work_dir"' EXIT

# run_deploy LOG_FILE [flyctl-deploy-args...]
#
# Tees flyctl's combined stdout/stderr to LOG_FILE and returns flyctl's own
# exit code (not tee's) via PIPESTATUS[0] — pipefail alone isn't enough here
# because we need the specific upstream code, not just "something failed".
run_deploy() {
  local log_file="$1"
  shift
  flyctl deploy "$@" 2>&1 | tee "$log_file"
  return "${PIPESTATUS[0]}"
}

# is_release_machine_startup_kill LOG_FILE
#
# True only when the log shows BOTH observed lines of the #834 signature.
# ANSI-stripped first (flyctl colorizes output). The exit-code regex is
# word-bounded so it can't false-match "exit code 1" or "exit code 1430".
# A real occurrence that's missing one of the two lines just stays red —
# that's the safe direction (under-retry, never over-retry).
#
# The gate is deliberately LOG-based, not flyctl-exit-code-based: flyctl
# itself exits 1 on a release_command failure (observed in run 31972609339 —
# "exit code 143" in the log, "Process completed with exit code 1" for the
# step), so the log line is the only place the release command's own 143
# surfaces. Do NOT "harden" this by requiring the wrapper's $exit_code to be
# 143 — that would silently disable the retry forever.
#
# Known accepted case: a migration that runs long enough to be SIGTERM'd by
# Fly's 5-minute release timeout also produces this signature and gets the
# one loud retry. That's fine — alembic migrations are transactional and
# every deploy attempt re-runs the release command anyway; the retry adds no
# new re-run semantics, and a second timeout still fails the deploy.
is_release_machine_startup_kill() {
  local log_file="$1"
  local stripped_file="$work_dir/stripped.log"
  # $'…' quoting injects a real ESC byte — GNU sed understands \x1b but BSD
  # sed (macOS, where these tests also run) would treat it as literal "x1b".
  # Grep a file rather than `printf … | grep -q` — with pipefail, grep -q
  # exiting at first match can SIGPIPE the producer on a large log and turn
  # a genuine signature match into a bogus non-match.
  sed -E $'s/\x1b\\[[0-9;]*[A-Za-z]//g' "$log_file" > "$stripped_file"

  grep -q 'has state: destroyed' "$stripped_file" || return 1
  grep -qE 'release_command failed running on machine [0-9a-f]+ with exit code 143\b' "$stripped_file"
}

# emit_retry_notice LOG_FILE
#
# Unconditional GitHub Actions warning annotation to stderr, plus an
# optional job-summary block (only when GITHUB_STEP_SUMMARY is set — local
# runs don't have it and must not crash).
emit_retry_notice() {
  local log_file="$1"

  # Wording is deliberately hedged: the signature proves "release machine
  # SIGTERM'd + destroyed", not WHICH SIGTERM. Usually it's the #834 startup
  # race, but a migration killed by Fly's 5-minute release timeout produces
  # the same lines — don't assert a root cause the log can't prove.
  echo "::warning title=Fly deploy retried::release_command machine was SIGTERM'd (exit 143) and destroyed — retrying once. Usually the Fly platform lifecycle race tracked in issue #834; if this recurs, or the timing wasn't a seconds-after-start kill, investigate before trusting the green." >&2

  if [[ -n "${GITHUB_STEP_SUMMARY:-}" ]]; then
    {
      echo ""
      echo "## Fly deploy retried (issue #834)"
      echo ""
      echo "The release_command machine reported \`has state: destroyed\` and"
      echo "\`exit code 143\` (SIGTERM). Retried automatically, once. This is"
      echo "usually the Fly platform-side lifecycle race tracked in #834 (kill"
      echo "seconds after start), but the same signature also fires for any"
      echo "other SIGTERM kill of the release command — e.g. Fly's 5-minute"
      echo "release timeout on a hung migration. Check the log tail below:"
      echo "if the machine ran for more than a few seconds before dying,"
      echo "investigate rather than trusting the retry's green. See"
      echo "agents/DECISIONS.md (2026-08-17)."
      echo ""
      echo "Last 40 lines of the failed attempt:"
      echo ""
      echo '```'
      tail -n 40 "$log_file"
      echo '```'
    } >> "$GITHUB_STEP_SUMMARY"
  fi
}

attempt1_log="$work_dir/attempt1.log"
run_deploy "$attempt1_log" "$@"
exit_code=$?
[[ $exit_code -eq 0 ]] && exit 0

if is_release_machine_startup_kill "$attempt1_log"; then
  emit_retry_notice "$attempt1_log"
  sleep "${FLY_DEPLOY_RETRY_DELAY_SECONDS:-5}"

  attempt2_log="$work_dir/attempt2.log"
  run_deploy "$attempt2_log" "$@"
  exit_code=$?
  exit "$exit_code"
fi

echo "flyctl deploy failed with exit code $exit_code; signature did not match the #834 release-machine startup kill — not retrying." >&2
exit "$exit_code"
