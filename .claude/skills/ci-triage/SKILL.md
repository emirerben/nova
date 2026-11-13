---
name: ci-triage
description: Triage red CI on a Nova PR — find which checks failed, separate infra flakes, cancelled runs and gate jobs from real failures, and reproduce each real failure locally with the matching command before fixing it. Use proactively, without being asked, whenever `gh pr checks`, `gh run watch/view`, a merge-guard block, a SessionStart note or the user shows a failed, cancelled or timed-out check; also on "CI is red", "why did CI fail", "fix the build", "checks failing". Do not push speculative fixes or re-run whole workflows before running this.
---

# ci-triage

Turn a red PR into a short list of actions: rerun one flaky job, fix a PR-body
gate, or reproduce and fix a real failure locally. CI is the slowest loop we
have (iOS shards and API suites run 20–40 min), so every wasted push is
expensive. The goal is **one push that goes green**.

## 1. Collect

```bash
python3 .claude/skills/ci-triage/scripts/triage.py [PR]   # default: PR of current branch
```

It prints, per red check: kind, workflow, job link, a log excerpt and a
`reproduce:` command. It is read-only. It uses `gh pr checks`, and falls back to
the REST API where GraphQL is unavailable (remote sessions). Use `--json` for
machine output and `--lines N` for longer excerpts.

If everything is pending, say so and stop. Do not poll in a loop. Use
`gh run watch <run_id>` once if the user is waiting.

## 2. Act by kind

Handle every red check, in this order:

| kind | what it means | do |
|---|---|---|
| `gate` | Aggregate job (`lint`, `test-api`, `build-and-test`) that only reports an upstream job | Nothing. Fix the upstream checks it names. |
| PR-body gate (reproduce starts "PR body / metadata gate") | `PR evidence`, the evidence step inside `lint`, `Require local test`, `Migration staging guard`, release metadata | Read the log line, fix the PR body or metadata per `docs/runbooks/pr-acceptance-checklist.md`. A `patch_id does not match` error means the diff changed since the receipt was written: rerun `python3 scripts/ci/pr-evidence.py --patch-id --base origin/main --head HEAD` and update the body. A rebase or body edit alone never needs this. No code change. |
| `infra-flake` | Runner shutdown, lost runner, disk full, 5xx, simulator boot failure | `gh run rerun <run_id> --failed` **once**. If the same job flakes again, treat it as `failure`. |
| `cancelled` | Superseded by a newer push, or someone cancelled it | If a newer run of that check exists and is green or pending, ignore it. Otherwise `gh run rerun <run_id> --failed` once. |
| `timeout` | Hit `timeout-minutes` | Treat as `failure`: look for a hang or a slow test (`--durations=20` output), never just raise the timeout. |
| `failure` | Real failure | Section 3. |

## 3. Reproduce, then fix (real failures)

1. Work in the PR's worktree (CLAUDE.md "Session workflow"), never the shared checkout.
2. Run the `reproduce:` command and **see it fail locally first**. For API failures the helper already narrows to the failing pytest node ids, so avoid rerunning a whole shard.
3. If it passes locally, do not push a guess. Compare with CI: Python 3.11 on Linux, the `-n auto --timeout=60` flags from `scripts/ci/api-tests.py`, env vars set in the workflow file, test-order effects (re-run the full shard: `cd src/apps/api && python3 ../../../scripts/ci/api-tests.py <shard>`).
4. iOS checks need macOS + Xcode (`make ios-verify`, or `KRIA_IOS_TEST_MODE=unit bash scripts/ios/verify.sh` for unit only). When the session is not on macOS, download the result bundle instead: `gh run download <run_id> -p 'ios-native-test-diagnostics*'` and read the failing test from the `.xcresult` / logs.
5. Fix the cause, re-run the same command until green, then `bash scripts/preship-check.sh` before pushing.
6. Push once, with all fixes for that red state together.

## 4. Report

Finish with a compact table for the user: check → kind → action taken → status
(fixed locally / rerun requested / needs you). Name anything you could not
reproduce and why.

## Never

- Merge, `--admin`, or bypass `scripts/hooks/merge-guard.py`. Only the user merges past red.
- Add `[skip-…]` escape-hatch markers, skip or delete tests, raise timeouts, or loosen a guard to get green without the user's explicit OK.
- Re-run a whole workflow repeatedly, or re-run a real failure hoping it flakes green.
- Ignore the repo rule "2 fix PRs on a ticket ⇒ failure-mode list before the 3rd" (`docs/runbooks/linear-workflow.md`).
- If `main` itself is red on the same check, say so: the repair goes to `main` first.

## When it runs automatically

`scripts/hooks/ci-triage-nudge.py` (wired in `.claude/settings.json`) tells
Claude to invoke this skill after any `gh` CI read that shows red, once per
session per red state, and at session start when the branch's PR is red.
`merge-guard.py` points here when it blocks a merge. Guards:
`scripts/ci/test_ci_triage.py`.
