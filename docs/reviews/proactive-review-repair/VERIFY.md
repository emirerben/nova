# Verification

The clean slice is based on timing dependency PR #1952 (`bec8537fd`). It consumes that PR's parser, timing allocator and compiler without duplicating them.

## Executed locally

- 319 tests passed against a disposable PostgreSQL 18 database: the new review/coordinator failure matrix, actual planner and parser replay, compiler and draft history, existing duration/sequence cases, recovery cases and legacy ownership guards.
- The actual planner journey replays model responses and the paid ledger only. It submits a request, parses a faulty copilot plan, makes one independent selector call, compiles and stages 27 unchanged words over 12 seconds, persists review provenance, and restores the exact previous snapshot with Undo. The published Job remains unchanged.
- The provider callback acquires real NOWAIT Job, draft and thread locks, establishing that the review does not hold their write locks.
- Negative cases cover rejected or malformed review, changed wording requiring confirmation, unsupported operations, duration-only intent, intentional timing patches, preservation requirements, stale generation/head/client/brief, missing capability, budget and provider failures, and a duration too short for the existing words.
- Independent review passed 57 tests against PostgreSQL, including the actual planner/draft/Undo replay and scoped compiler refusal. Two later controls cover missing sources and duplicate delivery; final parent verification passed 319 tests. Exact-head CI is recorded in the PR when complete.

These are offline model replays, not evidence that a live model understands the request. No paid provider calls were made.

## Native evidence boundary

Earlier synthetic native before/after MP4s and screenshots demonstrate the shared sequence compiler/render primitive. They were generated before this runtime orchestration was corrected and do not prove this new coordinator ran on a phone or edited the original 29 Ekim project. Those Library artifacts were already delivered separately. Live proposal quality, full rendered approval/Undo acceptance, original-footage playback and phone acceptance remain unproven.

No production flag, credential, budget or TestFlight rollout was changed.
