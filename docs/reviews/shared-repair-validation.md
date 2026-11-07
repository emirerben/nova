# Shared native repair validation

This is a local-fix receipt for KRI-1230, not completion of that feature or a
claim of device export, live-provider execution, rollout, or TestFlight delivery.

## Executed revision and artifact identity

Product source and all test assertions were pinned to
`7121c702772a6ca52c1b54413a291016515894c9` throughout the final serial batch.
Main base was `a518aed8c86f6ed9a17004327141f252eb395502`.
The complete iOS subtree was `d916a827c8d633455d69db5d5cf3e4f65339de9a`.
This report is a subsequent documentation-only addition; it does not relabel its
own commit as the revision that ran locally. Required PR CI must verify the
published head before readiness or admin merge.

The source correction and tests had independent semantic review. Integration
review confirmed newer main files were preserved and the tested iOS tree and
formal model were unchanged. Independent artifact audit checked actual test
logs, result-bundle summary, named formal outcomes, and exact execution SHAs.

## Native results

Xcode 26.6 (17F113), iPhone 17 Pro simulator, iOS 26.5 (23F77), arm64.
No physical device, production account, server flag or manual deployment was used.

| Stage | UTC on November 7, 2026 | Result |
| --- | --- | --- |
| Focused owner-boundary regressions | 05:19:03–05:21:08 | 4 passed, 0 failed |
| Eight-suite aggregate | 05:21:18–05:22:42 | 554 passed, 0 failed, 0 skipped, 0 expected failures |
| Bounded formal companion | 05:22:49–05:23:06 | 33 expected outcomes, exit 0 |

The aggregate used `xcodebuild test -project src/apps/ios/Kria.xcodeproj -scheme
Kria -skipPackagePluginValidation` with an existing iOS 26.5 simulator,
`-parallel-testing-enabled NO -test-timeouts-enabled YES
-default-test-execution-time-allowance 180
-maximum-test-execution-time-allowance 300`, unique result bundles and these
`-only-testing:KriaTests/` suites:

- NativeEditorSlotIdentityTests
- NativeEditorSessionTests
- NativeEditorMergeTests
- SlidePostTests
- SlidePostAIRecoveryTests
- SlidePostAISessionRecoveryTests
- SavedEditorRecoveryTests
- ChatDraftStagingTests

The focused run selected these NativeEditorSessionTests methods:

- testPromptRefreshDropsSuccessfulReadsAfterOwnerChange
- testPromptRefreshDropsFailuresAfterOwnerChange
- testPromptRefreshStillAdoptsSameOwnerResponses
- testPromptRefreshRecoveryRetainsOwnerAcrossTimerAndForegroundWake

Those four methods exercise 29 scenarios: 10 successful old-owner response
rejections, 6 failed-response rejections, 5 same-owner response controls, and
8 timer/foreground recovery scenarios including anonymous-owner controls.
Timer negatives await completion of the captured retry task rather than a timing
window. No test assertion was weakened. Local logs, exact command arrays and
xcresult bundles remain retained by the task; remote CI diagnostics must supply
independently accessible final-head artifacts.

## Behavioral negative controls

Tests-before-fix revision `7cb3dcbebff70c2af3df00a3e0b35af883ab667c`
compiled and ran the three response methods. Both rejection methods failed with
22 behavioral assertions; the same-owner control passed. Old-account responses
changed the editor from generation g1 to g2 or changed its target-loss state;
some paths issued follow-up reads after account replacement or sign-out.

Revision `b2a18793c054f43dcd8fd9bd0fc650374a06f6df` includes the in-flight
response fix but precedes the scheduled-recovery fix. The deterministic timer and
foreground recovery method failed with 8 behavioral assertions, including an
extra draft read and document replacement; the other three methods passed.
These are behavioral failures, not build, discovery or environment failures.

The source now retains account authority through suspended reads, baseline
retries, timer wakes and foreground/network recovery. Existing operation identity,
sequence, cancellation, clean-baseline, save/gesture and review guards remain.
Optional recovery scope distinguishes no pending recovery from a valid nil owner.

## Formal scope and controls

`python3 formal/ios_edit_flow/verify_merge_decision.py` ran serially after native
execution with Java 21 and the existing pinned TLC v1.8.0 jar. SHA-256:
`fc6878c603f0fdc18b9fe3e5bf51860cb90ab5b1154e3c371660c036d6bdd63e`.
Each run had a 120-second limit; the outer batch limit was 15 minutes.

All 33 expected outcomes completed: safety cases, policy mutants, positive
reachability, both queue-progress checks and their NoRearm controls, and both
prompt/latest owner guards with individual guard-removal controls. Removing only
the prompt-owner check produced the expected four-state
BeginRead → ChangeOwner → CompleteRead violation. The unchanged check passed.

Queue progress is conditional on stable owner/lifetime, eventual user
quiescence, and fair deferred rearm/drain execution. The companion models one
observation and opaque complete documents. It does not
prove JSON merge/serializer fidelity, arbitrary retry/catch implementation, or
the real sign-in UI lifecycle. Native replay and source review cover the scoped
implementation boundaries. Existing default formal models, historical controls
and coverage hashes were not weakened or blanket-resynchronized; the previous
as-is verifier's 36 drift entries remain separately qualified.

## Remaining acceptance

Full required PR CI, native UI/journey/render checks and durable remote result
artifacts must pass before merge. Full account-switch UI/live-provider replay,
KRI-1230 feature fulfillment, real-server re-record/new-take selection, physical
render/export and rollout remain unverified; these local unit/model results do
not establish them. The broader incidents remain open with their owners.
