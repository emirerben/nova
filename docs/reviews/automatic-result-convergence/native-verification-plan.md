# Final-source native verification selection

No native, package, simulator, device or model launch is authorized by this plan. The parent assigns the lane after a fresh process/disk/memory preflight.

Selection artifact: [native-selection.json](native-selection.json). It records source hashes and method candidates; it is **not test discovery or execution evidence**. The final checkpoint SHA/tree must be recorded with the run and checked against those hashes.

The source inherits main `dbb260e47ef640b8c0908808df640723e0f8c1a2` through integration `789789214046faf15c5e944c2d85cc5badc35666`, including landed Cunda. Backend delivery checkpoint is `083ededa04b59285c05783d4c8f646ad44a8bc59`. Earlier `4cbe7d2a`/190-method selections and old successful builds are superseded.

## App unit selection

Run each whole class from target `KriaTests` on a fresh final-source build. This includes preserved Cunda cases, source/Save/merge recovery, scoped pacing, Stop/lifetime changes, strict AI and export observation.

| Class | Source method candidates |
| --- | ---: |
| AuthoredMergeTests | 11 |
| EditorSaveHistoryTests | 7 |
| NativeEditorMergeTests | 24 |
| SlidePostMergeTests | 9 |
| ReviewApprovalIdentityTests | 3 |
| ReviewPlanTests | 20 |
| NativeEditorSessionTests | 271 |
| ChatDraftStagingTests | 85 |
| SavedEditorRecoveryTests | 31 |
| DeviceRenderTransientTests | 56 |
| SlidePostAIRecoveryTests | 3 |
| SlidePostSubmissionRecoveryTests | 9 |
| SlidePostSessionRecoveryTests | 29 |
| SlidePostExportTests | 57 |
| SlidePostTests | 39 |
| SlidePostChatEditTests | 23 |

Total: **677 source candidates in 16 classes**. Confirm the actual discovered and executed methods from xcresult. A selector, manifest entry or aggregate pass count cannot prove that a named critical case ran.

## Separate package selection

Run the entire `KriaMediaEngineTests/DeviceRenderCoordinatorTests` class from the same source. There are **39 source candidates**, including durable stage-before-export, legacy/unknown-stage refusal, intact original-attempt publication and missing-output protection. The package tests are not part of the app scheme's KriaTests/KriaUITests targets; they need a separate serial package test phase under the same resource allocation. Do not install or execute unverified dependencies.

## Offline UI selection

Run these six exact fixture methods after the same-source build. No private project, real provider, physical Photos library or hardware bench is involved.

- `KriaUITests/CreationUITests/testReviewPlanFlagsUpdatesAndUndoesASectionFromTheFeedCTA`

- `KriaUITests/CreationUITests/testReviewPlanUndoAllPutsEverythingBack`

- `KriaUITests/SlidePostUITests/testSlidePostCreationIsTheRichLayoutThenCreateThenKriaPropose`

- `KriaUITests/SlidePostUITests/testNewlyReadyAssetBecomesASlideWithoutTapping`

- `KriaUITests/SlidePostUITests/testFailedAIRequestShowsAClearMessageAndRetryKeepsTheMessage`

- `KriaUITests/SlidePostUITests/testAIRecoveryRefusesACompletedReceiptForAnotherRequest`

The historically named AI failure/retry case now verifies automatic original-result GET recovery after a lost POST response without tapping Retry. Its fixture permits one original UUID and one fake execution; the negative case returns a different receipt UUID. Each test launch gets an isolated synthetic account namespace, while relaunching the same app preserves its namespace for persistence checks.

## Build and proof requirements

Use repository project generation and xcodebuild flags from Makefile/scripts/ios/verify.sh: Kria scheme, Debug, CODE_SIGNING_ALLOWED=NO, -skipPackagePluginValidation, one assigned simulator, serial testing and at most two build jobs. Do not boot a simulator or start xcodebuild/SwiftPM until assigned. Generate fresh build-for-testing products and use only that source-bound xctestrun for test-without-building. The old /tmp/nova-auto-update-dd receipt predates these fixes and is unusable as proof.

Use task-isolated derived data/results and record toolchain, destination, final commit/tree, cache-input fingerprint, commands, exit codes, xcresult and per-method statuses. Inspect every requested critical case for actual execution and nonzero outcome. Treat unexplained skipped, undiscovered or zero-test selections as unresolved, not success. The six new native admission tests and new package stage controls must execute; source presence is insufficient.

Keep render/export proof skipping disabled. A bounded selected run does not replace required wider CI/render evidence, supported Linux evidence-decoder checks, shared model correspondence/TLC or backend aggregate gates. The shared model owner controls model/coverage maintenance. Physical output, actual Photos transactions and the private Cunda video remain separate, unperformed evidence.

Planning estimate only: fresh serial app compilation may require 30–60 minutes under the previously observed resource pressure, plus roughly 10–20 minutes for selected unit/package/UI phases. These are not measurements of this final source. First perform fresh resource preflight; no process may be interrupted arbitrarily.

## Remaining publication gates

Native/package/UI compilation and execution remain held. Preserve exact incoming speed/split semantics only after coordinated ready heads; do not stack unpublished candidates. API/worker/beat and migration 0138 must be ready before enabling durable AI admission and exposing the new client. Unknown paid outcomes never replay; legacy pending rows missing the original envelope reject explicitly. No publication/admin merge readiness follows from source review or this selection alone.
