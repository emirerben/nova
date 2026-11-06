# SlidePost read-only continuation (RB12–RB15)

Base: integrated main/Cunda `c0ab9630a3abdaf59c3892ed6bc9ed90ce8c4c74`; intermediate unreviewed WIP checkpoint `56b1ddc4649b2f14c95fdc1da6b5de08edc75783`. This records uncommitted final source, not a tested build.

## Behavior and boundaries

- RB12: exhausted AI observation continues against the original UUID. Its 40-read and three-POST budgets never reset. Atomic journal reservation permits a slow GET at most every30seconds, subject to the later saved Retry-After deadline. Slow pending/404 responses cannot enter the original-intent POST resumption branch. Cached completed results retain existing atomic local snapshot/applied-ID delivery rules.
- RB13: exhausted generation observation keeps a separate workspace observation condition. A persisted30second cycle reservation gates state and original receipt GETs, including when the original POST response was lost and no receipt was cached. A ready projection is not exportable while the durable submission lacks the matching current receipt. Confirmed supersession continues until the successor projection is observed.
- RB14: initial state-read exhaustion retains visible observation intent even without state or a journal. Further three-attempt GET cycles are paced by30seconds and any longer server deadline. No mutation is dispatched by this observer.
- RB15: an already authorized export changes from the initial readiness pace to30second GET checks after its fast budget. Account/environment/item/project/lifetime, draft content/version and generation target changes cancel that intent. Final pre-Photos revalidation may retry transient GETs only. Photos writes, local encoders, uploads, downloads and permission handling have no new replay loop. A terminal failure needs exact current receipt evidence when a durable generation submission exists.
- One cancellation-aware read lease serializes state, AI receipt and generation receipt GETs. Pending ordinary refreshes coalesce behind the slow workspace owner. Stop/background/dismiss invalidates observer callbacks; receipt identity and paid consent remain durable for later recovery. Late Retry-After is retained for its original journal even when its UI lifetime ended.

The continuation is intentionally unbounded in elapsed time while its visible/authorized intent remains current, but bounded in request rate and concurrent readers. It does not guarantee completion when the server can never produce a result. Server unknown outcomes remain unknown; no replacement UUID is minted automatically.

## Evidence limits

No native test, compiler, package, simulator, model, provider or PostgreSQL command ran for this delta. `git diff --check` passed. All methods below are written only. Existing finite-export-failure fixtures now cancel their injected read wait rather than expecting the retrieval budget to abandon the intent. Existing suspended-read fixtures queue a successor before releasing the original reader to match serialization.

The list contains16 new scenarios plus one renamed cancellation scenario. Earlier backend205/AI150/PG10 receipts predate this native delta and do not validate it. Independent static review and eventual coordinated native build/test/UI execution remain required. No performance or user-journey pass is claimed.

## Conditional admission boundary

The backend PENDING row has no guaranteed worker: a crash between reservation and permanent claim, or a NOWAIT-busy claim, may leave it unclaimed. A pure GET cannot make that row complete. The slow observer does not POST. Existing explicit recovery may observe and then resume the SAME UUID only for pending/404; its label is now “Check or resume original”, not a claim of pure GET. Known running/unknown receipts use “Check original result”. A replacement UUID is never created by either recovery action. The no-click completed-result claim applies to already admitted work that can eventually produce a receipt, not an unclaimed pending operation. Parent decision on that admission boundary remains separate from this read-only continuation.

Retry-After normalization rejects NaN/infinity/negative values, retains representable positive long deadlines, and sleeps in chunks of at most one day. AI wakeups use the saved deadline and fast3s/slow30s cadence. The underlying read gate, not the chunk duration, determines network eligibility.

State GET terminal errors (for example404 deletion or403 denial) end automatic state/export waiting with an actionable error; receipt GET404 stays uncertain. In-memory cooldown identity includes owner/environment/item so an old owner's late429 cannot throttle a replacement owner of the same item.

## Written method manifest

### SlidePostAIRecoveryTests.swift

- `testExhaustedAutomaticObserverGetsLaterResultWithoutPostOrReopen` — written, not executed
- `testExhaustedMissingAIReceiptNeverResumesPostAndStopFencesLateCompletion` — written, not executed
- `testLateRetryAfterSurvivesObserverStopAndRestart` — written, not executed
- `testSlowObserverRejectsWrongReceiptAndChangedOwnerWithoutNewWork` — written, not executed
- `testStoppedObserverDoesNotReadOrApplyCompletedResult` — written, not executed


### SlidePostSubmissionRecoveryTests.swift

- `testExhaustedMissingReceiptObservationNeverPostsAndPersistsPacing` — written, not executed
- `testExhaustedReceiptObserverFindsReadyWithoutCreateOrReopen` — written, not executed
- `testExhaustedUnknownReceiptCannotExportAnUnverifiedReadyProjection` — written, not executed


### SlidePostExportTests.swift

- `testAuthorizedExportObservesBeyondFastBudgetAndWritesPhotosOnce` — written, not executed
- `testExportDismissDuringSlowReadNeverWritesPhotos` — written, not executed
- `testExportStopsOnNewVersionOrTargetWithoutPhotosWrite` — written, not executed
- `testFinalExportRevalidationRetriesOnlyGETBeforeOnePhotosWrite` — written, not executed
- `testInitialReadExhaustionRearmsWithoutExistingStateOrMutation` — written, not executed
- `testLateOwnerCooldownDoesNotThrottleNewOwnerOfSameItem` — written, not executed
- `testReadObservationDelayHandlesMalformedAndLargeHintsWithoutShorteningAdmission` — written, not executed
- `testRenderThatNeverFinishesKeepsObservingUntilCancelled` — written, not executed
- `testTerminalStateDeletionEndsExportWithoutMutationOrPhotosWrite` — written, not executed


## Source fingerprints

- `src/apps/ios/Kria/Core/SlidePost.swift`: `acefc280086518452d96a1e703c8ff8d226c41c98c91370bde17b51ea042cf45`
- `src/apps/ios/Kria/Core/SlidePostAIJournal.swift`: `18c33ea2b6262bbe81cc9053681e21b0df978294b61a9017520ea5109d1354d7`
- `src/apps/ios/Kria/Core/SlidePostSubmissionJournal.swift`: `f10b19e23760c4a2af09d1ad609db5bbce641775719314aaedc0d39b2d32336c`
- `src/apps/ios/Kria/Features/SlidePostWorkspaceView.swift`: `7e667f4aed16fd4b90b72b34894745d217eda10ebb796e310e67f2c7005bf1b3`
- `src/apps/ios/Kria/Features/SlidePostExport.swift`: `0a11e2778b924567d32099ae25a7b34d57bffac5fef1b2f2dbfd4ccdbcfe4a04`
- `src/apps/ios/Tests/KriaTests/SlidePostAIRecoveryTests.swift`: `2f83a7e0d3cf4afd30a961e4f997709592a364aec818e9cd9003cd74d4ad9073`
- `src/apps/ios/Tests/KriaTests/SlidePostSubmissionRecoveryTests.swift`: `9eb54c09bb8316e92e0cc0b89c2e215be03b0e500471c774f584e4e4d6dbdc74`
- `src/apps/ios/Tests/KriaTests/SlidePostExportTests.swift`: `ca50c2269354e0c962ed0a1a20e47b113f826860fde57d6de2e35bfb8b8a842f`
