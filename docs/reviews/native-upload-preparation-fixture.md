# Native upload preparation fixture

Main iOS run `39915136904`, source
`cc0942febca1d755c9a5d0e7bf7b8bcd6ade80bc`, failed the reservation and record
waits in `BackgroundUploadRetryTests/testAClipBeingPreparedBlocksContinueAndIsNotCountedTwiceOnceRecorded`.
The case took 47.737 seconds and logged AVFoundation errors. Smoke and release
dispatch did not run. The separate backend deployment succeeded.

This admission scenario used `Data("clip asset-1".utf8)` as an MP4 while expecting
successful preparation. Selection reads capture metadata before reservation;
preview and duration readers also inspect the source. The candidate uses the
existing bundled `montage.mp4`, following the neighboring thumbnail scenario.
All assertions, five-second waits, admission rules and runtime code are unchanged.
Invalid-media handling remains covered by `CreationMediaPreviewTests`.

## Verification and limits

- The unchanged case passed once locally, then ten repetitions passed in 0.521
  seconds total. These do not reproduce the hosted failure or prove its cause.
- The candidate passed all 50 `BackgroundUploadRetryTests` and eight
  `CreationMediaPreviewTests` on an isolated iOS 26.5 simulator.
- A controlled `reservedCount = 0` mutation made the original and candidate
  fixtures each fail the same two count/Continue assertions. Observed detection
  was 1/1 before and 1/1 after, not a general statistical guarantee. The temporary
  production mutation was restored byte-for-byte before final verification.
- The failed hosted artifact used iOS 26.2; local passes on 26.5 do not establish
  an infrastructure cause. This is a fixture-contract correction, not a claim
  that a product upload bug or the hosted failure is conclusively resolved.
- Fresh exact-head hosted CI remains required. No retry of the old workflow,
  production setting, model call, account operation or physical-phone action.

Retained local logs and result bundles use the prefix
`/private/tmp/nova-native-upload-`; the downloaded failed artifact is under
`/private/tmp/nova-1763-ios-failure-diagnostics`. No media or result bundles are
committed.
