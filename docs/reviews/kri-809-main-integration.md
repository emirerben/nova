# KRI-809: current-main integration

The original fix is Yasin's PR #1737 at
`3d90628b7152ece4d695d73ba8a424512688b3ab`. Chat-time song matches used a
creation-thread ID in a field that accepts only render-job IDs. The fix records
the creator and plan item instead, preserving the real job ID for render-time
calls. Its existing regression exercises the real planner, matcher runtime,
cost ledger, persistence and proposal-trace route with a canned model transport.

## Acceptance and integration

This refresh starts at fetched main
`e962030f339803ccd74ff03c4fd1869ccfc90d9c`. The author reported the work ready
for review; its head and description had no newer author update since
2026-10-21. A separate `codex/pr-1737-integration` worktree merges the exact
source commit without rewriting it. The original product code and test
assertions are unchanged. Only this integration report is added.

Retained CI on the original head failed seven shared M2 expectations and the
old Turkish-font clarification expectation. Main now contains their separately
reviewed repairs, #1735 and #1740. They are inherited through main rather than
copied, weakened, skipped or reimplemented here.

The original test file records eight failure modes before the original fix:
wrong/missing trace ownership, missing item propagation, cost attribution,
reservation ownership, repeat-call identity, render-time ownership, and logs.
Those remain the acceptance boundary for this refresh. This is a logging and
cost-control fix; it does not establish the original Don Julio edit's full
request fulfillment or actual video export.

## Focused verification

An isolated, migrated local PostgreSQL database was created with
`bash scripts/dev/test-db.sh`. Then:

```sh
bash scripts/check-api.sh test \
  tests/kria/test_kri809_chat_song_match_trace.py \
  tests/kria/test_kri679_music_mood.py \
  tests/agents/test_music_matcher.py -q --tb=short
```

Result: **38 passed, zero skips**. The provider transport is mocked; no paid
call or production write occurred. The regression retains actual trace rows,
creator-attributed reservations, repeated matches and render-time ownership.
Hosted CI must validate the published integrated head independently.

Existing source provenance and original failed runs remain in the PR. Its
normal review and merge requirements remain active. No phone operation,
production configuration, provider choice or manual deployment is changed.
