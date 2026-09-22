# KRI-145 editor media verification

Implementation: variant-scoped source admission, device timeline photo/video
imports, and media-only Visuals blocks on existing guided device edits.
`PHONE_EDITOR_MEDIA_ENABLED` remains off by default. No production flags,
TestFlight release, or issue closure are part of the code verification below.

## Automated evidence

- Backend admission tests cover attempt/lease recovery, broker failure, stale
  revisions, exact approved receipt reuse, stable indices, schema limits,
  ownership and generation replacement, invalid proxies, and safe errors.
- Save tests cover canonical source union/hash, unchanged approval, atomic failure,
  retained original footage receipts, media layers, reopen projections, text
  resave after timeline changes, import-flag rollback, and pinned narration.
- Compiler tests cover image overlap/order, video trim, fit/focal/zoom geometry,
  and explicit rejection of unsupported or overlong media treatments.
- Swift tests cover the HTTP contract, durable ready/failed placement recovery,
  photo and footage placement, undo/redo, cancellation and stale-generation
  fences, capacity, leaving the editor, and short-video duration bounds.
- A fixture-backed native export test passed for server-compiled contain/cover
  photos, overlapping layers, and a trimmed video, including local eligibility.
  This caught the native `alphaOverlay` requirement missing from the initial
  server recipe; it is now explicit and gated with the other verified features.
- Actual command results and any environment limitations are recorded in the PR.
  A passing simulator suite does not qualify physical-device performance.

## Verification run

The final verification run completed with the following results:

- Focused backend KRI-145 tests: 808 passed; after the final `alphaOverlay`
  capability fix, the affected set passed 166 tests and the adjacent set passed
  230 tests.
- `make verify-kria`: 420 passed, 7 skipped.
- The first full `make ios-verify` run completed with 491 unit tests, 11
  opt-in skips, and 62 UI tests passing. It needed one retry because of a
  timeline-scrub warning.
- The final direct Xcode run completed with 500 unit tests, 12 opt-in skips,
  zero failures, and two affected UI tests passing.
- A server-compiled native pixel-export target passed in the simulator. The
  captured log is `/private/tmp/kri145-media-export.log`; this is export parity
  evidence, not physical-device qualification.
- The final pre-ship check passed.
- CI exposed a visual-asset lock-order violation and an outdated compiler test
  stub. Both admission paths now lock item, asset, then job; the lock-order guard
  remains unchanged. After those fixes, 823 focused backend checks passed,
  including the canonical-lock and song-reference suites, and pre-ship passed.

Admission race tests exercise competing completion/retry interleavings with
mocked database boundaries. A live PostgreSQL concurrency exercise remains part
of the rollout check; the implementation serializes item/job/source updates with
row locks and rejects stale attempt tokens.

## Required before enabling imports

Use an existing saved guided edit with unsaved text and timeline changes. Perform
both Photos and Files imports through each button. Include portrait and rotated
landscape footage, a short video, a still, overlapping visuals, and an edit with
narration. For each path verify:

1. Preparation progress remains visible; interrupt upload/preparation, relaunch,
   and retry without duplicate placement or chat attachment.
2. Preview uses the retained original or exact Visuals asset. Existing unsaved
   edits survive the source-pool refresh. Undo/redo restores the same source.
3. Save succeeds, reopen shows the same arrangement, and export matches preview
   for geometry, timing, layer order, text, and audio.
4. Canceling import leaves the draft unchanged. A stale editor, replaced/deleted
   asset, unsupported proxy, and failed preparation preserve the previous saved
   document and render request.
5. Removing a local footage original produces the existing relink flow, with no
   proxy or cloud fallback. Rollback blocks new imports and preserves saved media.
6. Exercise two concurrent imports and a concurrent Save against live PostgreSQL;
   only the current revision/attempt may admit, with no reused source indices.

## Physical qualification record

Record the app build, backend SHA, device/OS, fixture duration, preview FPS,
seek-to-visible-frame p95, export time, peak memory, thermal state, and parity
captures using the [iOS performance gate](../../runbooks/ios-development.md#performance-gate).

| Target | End-to-end import / Save / reopen / export | Performance / thermal |
| --- | --- | --- |
| iPhone 13 class | Pending | Pending |
| Current iPhone | Pending | Pending |

An iPhone 13 Pro was visible to CoreDevice during implementation; connection alone
is not qualification. Physical iPhone 13-class and current-iPhone qualification,
including performance and thermal measurements, was not executed. Live PostgreSQL
concurrency validation was also not executed. Keep the feature off and KRI-145 open
until both targets and the existing-edit acceptance flow have evidence.
