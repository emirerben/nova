# Verification

The slice consumes the timing parser, allocator and compiler from #1952 without duplicating them.

## Executed locally

- The actual planner journey replays model responses and the paid ledger only. It submits a request, parses a faulty copilot plan, makes one independent selector call, compiles and stages 27 unchanged words over 12 seconds, persists review provenance, and restores the exact previous snapshot with Undo. The published Job remains unchanged.
- The provider callback acquires real NOWAIT Job, draft and thread locks, establishing that the review does not hold their write locks.
- Negative cases cover rejected or malformed review, changed wording requiring confirmation, unsupported operations, duration-only intent, intentional timing patches, preservation requirements, stale generation/head/client/brief, missing capability, budget and provider failures, and a duration too short for the existing words.

These are offline model replays, not evidence that a live model understands the request. No paid provider calls were made.

## Native evidence boundary

Earlier synthetic native before/after MP4s and screenshots demonstrate the shared sequence compiler/render primitive. They were generated before this runtime orchestration was corrected and do not prove this new coordinator ran on a phone or edited the original 29 Ekim project. Those Library artifacts were already delivered separately. Live proposal quality, full rendered approval/Undo acceptance, original-footage playback and phone acceptance remain unproven.

No production flag, credential, budget or TestFlight rollout was changed.
