# Verification

The slice depends on timing PR #1952, now on main. It consumes that PR's parser, timing allocator and compiler without duplicating them. It supersedes PR #1953, whose head was this same change plus main merges.

## Executed locally

- 319 tests passed against a disposable PostgreSQL 18 database: the new review/coordinator failure matrix, actual planner and parser replay, compiler and draft history, existing duration/sequence cases, recovery cases and legacy ownership guards.
- The actual planner journey replays model responses and the paid ledger only. It submits a request, parses a faulty copilot plan, makes one independent selector call, compiles and stages 27 unchanged words over 12 seconds, persists review provenance, and restores the exact previous snapshot with Undo. The published Job remains unchanged.
- The provider callback acquires real NOWAIT Job, draft and thread locks, establishing that the review does not hold their write locks.
- Negative cases cover rejected or malformed review, changed wording requiring confirmation, unsupported operations, duration-only intent, intentional timing patches, preservation requirements, stale generation/head/client/brief, missing capability, budget and provider failures, and a duration too short for the existing words.
- Independent review passed 57 tests against PostgreSQL, including the actual planner/draft/Undo replay and scoped compiler refusal. Two later controls cover missing sources and duplicate delivery; final parent verification passed 319 tests. Exact-head CI is recorded in the PR when complete.

These are offline model replays, not evidence that a live model understands the request. No paid provider calls were made.

## Port to current main (KRI-1171)

Re-run after cherry-picking onto main `370fcfeb4` against a disposable PostgreSQL database: the 58 repair tests, the 846 tests of the mapped reliability gates (`scripts/reliability-gates.py run --changed`) and 174 timing/editor-op neighbours passed. `tests/kria` passed 6850 with one environment failure (`test_runtime_postgres_integration.py::test_strategy_approval_gates_on_speech_cleanup_then_dispatch_receives_the_choice` needs Redis on `localhost:6379`, which was not running). `_complete_draft_turn` now computes `preserve_text`; the repair candidate is only built when no `preserve_editor_text` fact or `preserve_text` operation exists, so both compiles use `preserve_text=False` and the digest comparison holds.

## Native evidence boundary

Earlier synthetic native before/after MP4s and screenshots demonstrate the shared sequence compiler/render primitive. They were generated before this runtime orchestration was corrected and do not prove this new coordinator ran on a phone or edited the original 29 Ekim project. Those Library artifacts were already delivered separately. Live proposal quality, full rendered approval/Undo acceptance, original-footage playback and phone acceptance remain unproven.

No production flag, credential, budget or TestFlight rollout was changed.
