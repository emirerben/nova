# SlidePost strict AI recovery evidence

Working-tree base: `4cbe7d2a7a5b89d0decdbd4a1405d2323250aa7a`. This is uncommitted source evidence, not a commit receipt. Integration with main `e090eb668124fee58cfc74c34e662ac78e268342` remains pending.

## Acceptance and boundary

Lost AI responses recover the original operation and result without a second paid invocation. Owner, environment, item, canonical project and exact submitted input remain bound. Current local edits are merged against the original request baseline while the latest committed save baseline is retained. A completed paid result and its applied operation ID are atomically stored before marking its request delivered.

New strict POST/GET endpoints are capability gated. Existing legacy endpoints are unchanged. Pending original operations may be claimed once; running/unknown operations never execute again. GET is observation only. Canonical owned-item locks plus a NOWAIT thread lock in a savepoint avoid the two existing legacy lock-order directions; contention remains pending and releases claim locks. Claim and result commits precede paid execution and HTTP success respectively.

## Executed checks

- Behavioral negative control: `/tmp/test_slide_ai_legacy_red.py` expected one fake paid invocation after a lost response and a fresh-ID legacy Retry; observed two, one failing assertion. `/tmp/slide-ai-behavioral-red.log`. The retained legacy baseline intentionally still demonstrates two calls; the new strict control asserts one. Initial new-module collection failure is not claimed as behavioral red.
- Offline fake-provider suite: **150 passed, 44 warnings, 4.00s**, `/tmp/slide-ai-final-offline.log`. Command: `src/apps/api/.venv/bin/python /tmp/slide-ai-offline-tests.py -q src/apps/api/tests/routes/test_slide_post_ai_requests.py src/apps/api/tests/services/test_slide_post_ai_requests.py src/apps/api/tests/routes/test_slide_post_chat_edit_route.py src/apps/api/tests/services/test_slide_post_compose.py src/apps/api/tests/services/test_slide_post_chat_edit.py`. Runner denies all socket connections and disables dotenv/real credentials.
- Real PostgreSQL strict endpoint suite: **10 passed, 11.62s**, `/tmp/slide-ai-pg-final.log`. From `src/apps/api`: `/tmp/nova-ai-db-safe-python -m pytest -q tests/routes/test_slide_post_ai_requests_postgres.py`. Isolated `nova_auto_convergence_ai_20261106_test`, migration0137, exact loopback5432 allowlist, external sockets denied, pool size2/max overflow0. No real provider calls. Covers claim race, committed lost response after newer save, stale input, changed ownership/version after reservation, busy thread, unknown/late completion, uppercase native UUID and replaced project, rollout-off observation.
- Guarded contract generation `--write` and `--check` passed; `/tmp/slide-ai-contract-check.log`. Strict mobile routes and DTOs included.
- Targeted Ruff check and format check passed (10 AI/backend files); `git diff --check` passed.

## Native evidence limit

All 33 new methods below are **written, unexecuted**. No Swift compiler, native build, simulator, provider, formal or model run was launched for this final delta. Earlier native build receipts predate these changes and cannot prove them. Integration must compile and execute the named suites, then exercise the offline UI journey before merge.

Atomic local persistence uses synchronous file writes, skips identical encoded snapshots, and retains old unscoped UserDefaults bytes without treating them as ownership proof. Gesture latency and disk-failure UI have not been measured. No performance claim is made. File atomicity addresses process death, not a guarantee against storage hardware loss. Real provider semantics and paid-media output are outside the fake-provider evidence.

## Remaining explicit actions

- Normal transient retrieval automatically follows the original ID within persisted budgets and Retry-After deadlines.
- After bounded exhaustion, “Check original result” observes that operation; only a still-pending/missing original receipt can safely resume the same UUID, never a replacement.
- A new instruction is a new explicit operation. Definitive rejected/failed AI responses do not silently create paid retries.
- Overlapping edits require the visible proposal/conflict choice; disjoint edits stage automatically. Apply remains explicit.
- Render new work remains explicit after fresh exact terminal evidence; uncertain submissions recover the same durable key. Saved-version catchup is the separate backend owner's scope.

## New native method manifest

- `testPreparedIntentSurvivesRecreationWithExactIdentityAndInput`
- `testOldResultCannotReplaceNewExplicitIntent`
- `testJournalFailurePreventsReservation`
- `testLostResponseThenRetryReadsOriginalResultWithoutSecondPost`
- `testProcessRecreationRecoversVisibleResultAndPreservesConflictingLocalEdit`
- `testAccountReplacementCannotObserveOrApplyOriginalReceipt`
- `testWrongReceiptIdentityInputOrBaselineNeverStages`
- `testChangedDigestOfKnownReceiptNeverStages`
- `testLocalCommitFailureRecoversPaidResultAfterRecreation`
- `testFailedDeliveredMarkThenUndoDoesNotReapplyAfterRecreation`
- `testCachedCompletedResultAtZeroReadBudgetRecoversWithoutNetwork`
- `testOwnerEnvironmentAndProjectReplacementNeverRestorePrivateLocalState`
- `testLegacyUnscopedStateIsRetainedButNeverAdopted`
- `testPersistedRetryAfterSuppressesReadsAcrossRecreation`
- `testOldServerCapabilityPreventsEveryPaidEndpoint`
- `testOldServerKnownLocalProjectRestoresManualDraftWithoutAuthorizingAI`
- `testManualOnlyUnknownProjectNeverRestoresAIProposal`
- `testRolloutOffStillRecoversOriginalResultWithoutNewPost`
- `testLateOldReceiptCannotConsumeNewExplicitIntent`
- `testProjectReplacementDuringReceiptGetRejectsOldCallback`
- `testOldSaveCompletionCannotClearNewContextBusyState`
- `testDirtyRequestBaselineAndLaterConflictKeepManualCaptionOnApply`
- `testCapturedOwnerStopsAllBoundMutationsBeforeTransport`
- `testFailedAtomicReplacementRetainsOriginalIntent`
- `testCrossKindNewIntentRejectsOldResult`
- `testStaleWriterCannotShortenServerDeadlineOrResetBudget`
- `testExhaustedAutomaticBudgetAllowsOneExplicitOriginalIntentObservation`
- `testPreparedBeforePostResumesSameUUIDAndRecoversOnce`
- `testUnknownAndRejectedChecksNeverMintReplacementButNewInstructionCan`
- `testCompletedNoopAndFailedResponseDoNotOfferPaidRetry`
- `testRecoveredProposalKeepsLaterLocalCaptionAndNewServerVersionOnApply`
- `testFailedRequestReservationMakesNoPost`
- `testAccountAndItemReplacementDuringReadDiscardOldResult`

## Source fingerprint

- `src/apps/api/app/routes/slide_post_ai_requests.py`: `627f9474ddb4642a30025ebfda50814ee6d8af23e8bf25aa56ce215f4747bb82`
- `src/apps/api/app/services/slide_post_ai_requests.py`: `6565a8fe3bc97338d7ec3f4bf8de906f47546b68ae854aac29bc26619fb30dbb`
- `src/apps/api/app/migrations/versions/0137_slide_post_ai_requests.py`: `5949b89ef7e2fe3a69ab75c08d7ad60f2bb67174f6f26ad33d6e549a599dc6fa`
- `src/apps/api/app/routes/plan_items.py`: `034e31ca878547a57bc294506cb20bc1f93e9cd4dfb667c6995e5a8d77439ff3`
- `src/apps/api/app/services/slide_post_chat_edit.py`: `75e99ba2355113e813c86c4003d7620fe487ca2a5364ae15559a4bdf95af0bff`
- `src/apps/api/app/services/slide_post_compose.py`: `0c84d471004e979722bb7c6e1639d8d01e294237f038c9fa12cc243082909a81`
- `src/apps/ios/Kria/Core/SlidePost.swift`: `8e819aacd9d35edd6023ffeb39a98761431224cefcff8ede4314b0475a78ecd9`
- `src/apps/ios/Kria/Core/SlidePostAIJournal.swift`: `efecbce0396ecaec5e913a7ce816cd0148d102d60aff6abd151ccbadeda06b9e`
- `src/apps/ios/Kria/Core/Services.swift`: `485eafb8e82604029e6a11fc111ec2f880082c256d6eefbfb978804c1d31a2d8`
- `src/apps/ios/Tests/KriaTests/SlidePostAIRecoveryTests.swift`: `19c8e94ba37a11c7376070a94b6b16e79fe4a6abf28335fb85a350b03afdcc74`
