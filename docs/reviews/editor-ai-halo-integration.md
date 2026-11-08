# Local halo/session integration candidate

Latest landed-main candidate and validation limits: [KRI-1230 main integration](editor-ai-halo-main.md). Older integration/hold statements below record their original candidate.

This branch is local integration preparation, explicitly authorized against Cunda `bc0bfd6ee198a5b5ec3e2d0dc3276b1908c50a3c`. The standalone halo remains unchanged at `d8c93aa545609c1d272986d623645885487d255e`. Cunda's worktree and published branch were not edited. Publication and merge require Cunda to land, rebase onto the resulting main, and affected revalidation. Native execution remains held. Tracking is now [KRI-1230](https://linear.app/kria/issue/KRI-1230/show-ai-editing-activity-in-the-iphone-editor) in iOS editor polish. The earlier capacity failure is historical and resolved; native/dependency/publication gates remain open.

## Choice: session-owned old-item handoff

| Approach | Required work | Limits |
| --- | --- | --- |
| Immutable retained image | Obtain a correctly composed, owned frame at the displayed clock before a mutable composition changes. A cached scrub frame is not always present or current; UIKit/AVPlayerLayer snapshotting is not a reliable substitute for video pixels. | A new frame extraction path would add decoding/capture work and another asynchronous ownership boundary. Capturing every frame would be unacceptable. This candidate adds no image extraction. |
| Session-owned old item | Retain the admitted player/item, its composition/providers, displayed target/lifetime/generation/document/preview sequence and aspect ratio. Keep it alive while the actual replacement warms and reaches a ready display layer. | The retained composition must not be mutated in place, and `installPlayer` must not detach it prematurely. There may be one real, paused warming candidate; no extra player runs for decoration. |

The second approach reuses the existing player/compositor and readiness helper. It is narrower than a new screenshot pipeline and leaves the first-open admission fence intact. Native proof is still required for timing, frame content and resource behavior.

## Implemented interfaces and adapter rules

- `NativeEditorPreviewOwner`: project plus Cunda lifetime, item, job, variant and thread identity.
- `NativeEditorPreviewWork`: immutable captured owner, generation, document revision and preview sequence. `NativeEditorPreviewWorkTracker` distinguishes live work from a sequence advanced only to invalidate old work; its awaiting-frame phase survives composition construction.
- `NativeEditorSession.isPreparingCurrentPreview`: reads the explicit matching work ticket while the editor presentation is active. It covers cached and deferred rebuilds. Stale cleanup cannot clear a newer ticket.
- Admission comes through `previewFrameWasShown(player:ticket:)`, reached from the keyed `AVPlayerLayer.isReadyForDisplay` observer. It also requires a loaded, settled, displayable source preview and ready matching item. Seed URL / source `.ready` alone cannot admit a retained presentation.
- The private admission record keeps the actual item, player, composition/providers and aspect ratio. Same-target prompt synchronization can arm it after authoritative sequence/baseline checks. The first-open path, a new lifetime, and a different target do not borrow it.
- `previousPreviewPresentation` exposes display-only player/aspect/status. Work, previous-picture availability and previous-picture notice are separate: failure/cancellation stops work and replaces the updating notice with a failure/stopped notice. A valid previous picture may remain.
- Replacement warming now covers paused source handoff as well as the playing finished-render case. A moving clock is reseeked under the captured owner/document guards before adoption. A retained old item is paused but not detached on install; the new owned layer's admission retires it. In-place composition updates are refused during retention.
- Inline/fullscreen presentation uses the recorded aspect and previous-version notice; current scrub frames and stale canvas/Draw handles are suppressed. `canDisplayCurrentPlayer` and KRI-535's seed/staged/dirty admission rules are unchanged. Export UI, direct route selection and direct source export reject a retained previous presentation.
- Begin-load, target property replacement, cross-job discard, media-service reset and editor dismissal clear admission. They do not create a second editor lifetime. Return needs a new surface admission callback.
- The three new public lifecycle callbacks are registered as `not_an_edit` in the existing parity registry. New native files are mapped in the journey registry; no mandatory check was removed.

## Deterministic case matrix

Every native test below is written but **never run** under the shared-lane hold. Pure policy tests establish value transitions only; the session fixture tests use the real compiler/player preparation but substitute the external surface admission callback. Neither is an inspected native-output claim.

| Case | Required outcome | Written coverage / remaining proof |
| --- | --- | --- |
| Cached rebuild while source state stays ready | Activity persists through the real outstanding work and frame wait. | `NativeEditorAIActivityTests.testPreviewWorkStopsOnReadinessOrFailureWithoutInventingSuccess`; tracker/session tests. Changed-document UI proof pending. |
| Deferred gesture/coalesced build | Explicit queued work is pending; bare sequence mismatch is insufficient. | `NativeEditorPreviewHandoffTests.testCachedAndDeferredWorkNeedsAnExplicitLiveOwner`. Real gesture overlap pending. |
| Delayed candidate readiness | Keep previous presentation and work until the owned new frame arrives; never time a success. | `testDelayedFrameReadinessKeepsRealWorkActiveUntilAdmissionOrTermination`; `NativeEditorPreviewWorkSessionTests.testReadyCompositionStillWaitsForTheOwnedSurfaceAdmissionCallback`. Real layer/frame timing pending. |
| Old cancellation or cleanup after superseding | Cannot stop or commit the newer work ticket. | `testStaleCleanupCannotStopNewerWork`; candidate preroll race still needs native exercise. |
| Same target/generation reload | New Cunda lifetime drops old work and admission. | `testReloadAndTargetReplacementCannotBorrowAdmissionOrWork`; `testSameTargetReloadInvalidatesDelayedWorkAndItsStaleCleanup`. |
| Project/job/item/variant/thread replacement | Never show or restore a previous target; no cross-job retention. | Pure owner replacement matrix; real suspended replacement UI pending. |
| Generation or document revision changes | Reject a mismatched candidate/cleanup ticket; previous record retains its own displayed metadata. | Pure work-identity matrix; actual newer-generation frame observation pending. |
| Initial untrusted seed | No retained admission, even if its player can eventually load. | `testUntrustedInitialSeedCannotBecomeAnAdmittedSourcePreview`; first-open wrong-title visual control pending. |
| Terminal failure/cancellation | Stop halo immediately; a still-valid previous picture says failed/stopped, not updating. | `testFailureAndCancellationStopWorkButDoNotClaimPreviousPictureIsUpdating`, activity failure cases; real retained failure/retry UI pending. |
| Dismissal followed by late completion | Cannot resurrect retained presentation or active halo offscreen. | `testDismissalCannotBeUndoneByDelayedPreparation`; return/re-admission UI pending. |
| Old terminal render plus new-job rendering | Show actual newer-job activity. | Existing combined old synced/localReady/superseded/waiting projection cases. |
| Paused handoff | Actual new candidate is ready before retiring the paused old item. | Source reviewed; real paused-layer playback and pixels pending. |
| Playing/scrubbed handoff | Respect latest clock and playback intent; no stale warmup jump or audio overlap. | Source reviewed; real AVFoundation continuity pending. |
| Failure/retry/in-place mutation | Retained composition stays immutable; recovery never promotes it to current authority. | Source reviewed; session orchestration/retirement fixture coverage remains incomplete. |
| Export during a retained picture | UI and both direct export entrypoints refuse it as current. | Source guards reviewed; actual retained route/export negative control pending. |
| Inline/fullscreen | Same previous aspect/status, no current scrub still over previous video. | Independent reviewer found the original omission; source repair reviewed. Visual transition proof pending. |
| Reduce Motion / Reduce Transparency | Static outline and readable text/opaque glass; ownership behavior unchanged. | Existing normal/reduced/opaque UI cases written; not executed. |
| Unsaved edits and Undo | Keep Cunda merge/rebase/decision ownership; presentation does not replace document/history. | No new mutation path; real changed-document/manual-edit journey remains pending. |

## Review and evidence limits

Independent read-only review accepted the source-level ownership, stale-cleanup, retained-provider lifetime, export/canvas guards and avoidance of in-place mutation during retention. It found a fullscreen labeling/aspect/scrub omission, which was repaired. Its delivery-time readiness guard was added. The final focused pass also identified an optional document-revision type mismatch, missing delayed-fixture ownership fences, and a seed test that used the trusted default. All three were repaired and independently re-reviewed at source level: revision remains optional; post-delay writes and failure handling require the captured work token; the seed test explicitly rejects an untrusted URL. No native compiler/test was run by either reviewer.

One unresolved proof concern is explicit: an in-place local rebuild can reuse an item/layer whose `isReadyForDisplay` was already true. A new readiness ticket and the observer's initial notification prove item/layer availability, not independently that a newly composed frame reached the screen. This must be exercised with distinct visible frame content before declaring admission/handoff verified. The previous composition is never updated in place once retained.

Passed static checks: affected Swift syntax parsing, whitespace checks, registry path coverage, scoped Ruff and formatting, and the existing parity extractor reports all 298 public actions mapped with no missing/stale rows. These do not type-check the native app, execute the native tests or establish visual/runtime performance.

## Proposed bounded native slot

Use the existing `verify-kria-ios` helper to create this integration worktree's own simulator and build once, only after the parent grants the lane. Run the five `NativeEditorAIActivityTests`, five `NativeEditorPreviewHandoffTests` and four `NativeEditorPreviewWorkSessionTests`, then the three new editor activity UI cases and the existing conversation-sheet-return regression, serially with an `.xcresult` bundle and no hidden retry. Keep failed attempts and attachments.

That first slot establishes compilation and initial adapter/fixture viability. It does not close the changed-document picture gate: add/exercise a real delayed changed-document case (cached and uncached; paused and playing), inspect previous/new frames and clock, test failure/superseding/reset/export negatives, and capture normal/reduced-motion/transparency plus fullscreen evidence. Inspect actual pixels, then re-review fixes. No native command has run yet.


## Tracking and dependency update

The earlier authorized create retry for **Show AI editing activity in the iPhone editor**, in existing project **iOS editor polish** (`P-KRI-10`), reached Linear and failed with HTTP 400: "You've exceeded the free issue limit for this workspace." No issue identifier or URL was returned. No second retry, archival/deletion of existing issues, or substitute tracker was attempted. The earlier authorization denial is resolved; workspace capacity is now the blocker. PR evidence cannot be assigned a fabricated issue. After the user reported restored capacity, a fresh exact-title duplicate check returned no match and the newly authorized retry created [KRI-1230](https://linear.app/kria/issue/KRI-1230/show-ai-editing-activity-in-the-iphone-editor) on 2026-11-06 at 12:51 UTC. That real identity now owns the pending native proof. No split-screen issue was created by this task.

Cunda's parent reports published head `0997d4fe7e6a7768c023cb6e7bf538e68f588d8a`, containing two test corrections and evidence after this integration base. The inspected base `bc0bfd6` has production app subtree `c4ee10fcd6b6cdfe67941f55c98bbe68f241bc23`, matching the parent-reported production subtree of that newer head. This branch has not adopted the unmerged dependency update. Cunda must still land before rebase and final validation.
