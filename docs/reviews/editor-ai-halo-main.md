# KRI-1230 candidate on landed main

Tracking: [KRI-1230 — Show AI editing activity in the iPhone editor](https://linear.app/kria/issue/KRI-1230/show-ai-editing-activity-in-the-iphone-editor).

## Exact integration

- Fetched base: `c078f13b9ad6f0f60d4a5e94f4cd2afac551a76b`, including Cunda merge `70bff5f3738ca8f6873e3a85ddd9fbad374ada49` and #2109 Improve/suggestion cards.
- Branch/worktree: `codex/editor-ai-halo-main`, `nova-editor-ai-halo-main`.
- Source/layout/capture-test commit: `4bc31a31f66735031790daa126b1dc1cf0188a9e`. Later evidence-only commits do not change native source.
- Preserved comparison candidates: standalone `d8c93aa545609c1d272986d623645885487d255e`, prior integration `990f169fd0b0095b482585c149b82c7d3fdedc29`.
- Only the six halo commits after the old Cunda integration base were transplanted. No Cunda implementation/test/release commits were replayed. Current repository instructions and applicable native/motion/evidence guidance were inspected.

## Conflict and overlap resolutions

The two textual conflicts were adjacent declarations and initializer assignments in `NativeEditorView`: keep both `improve` and `conversationActivity`. The chat call keeps `improve: readyCardsPresentation` and adds only the activity projection. `Services.swift` is byte-for-byte unchanged from the fetched base.

The retained Improve sheet, checked-card tap, dismissal-then-conversation flow and suggestion ledger remain in charge of their original state. Cunda's load epoch, guarded editor-open fallback, dirty-state/retry UI, first-open seed admission and target/generation checks remain intact.

Two independent bottom-corner overlays could overlap once the halo button expands. They now share a width-constrained bottom HStack with Improve given layout priority and the activity label allowed to wrap. Button identifiers/actions, corner padding and sheet behavior stay the same. Halo animation also pauses while Improve is presented. A UI regression checks separated, on-screen, hittable controls during a real fixture POST and verifies Improve still opens.

## Completing actual-app capture cases

Added `testEditorAIActivityCompletingLifecycleCapture`, `testEditorAIActivityCompletingReducedMotionCapture` and `testEditorAIActivityCompletingOpaqueCapture`. They use identical seeded project/media/prompt and the existing offline HTTP fixture. A distinct `completingFollowup` option selects `KRIA_CHAT_THOUGHTS=1`; the transport delivers the correlated terminal response after six delta ticks. The existing held-turn option still selects `hold` for its own inspection case.

Each completing case attaches real app idle, sending, working and settled screenshots only after assertions, checking preview/play availability and absence of a previous-preview notice at settlement. These tests have never run. There is no implemented-after image or movie yet. Historical installed build-1902 before remains separate and is not pixel-matched to the simulator fixture. These captures do not establish a real changed-document handoff or live-model/export result.

## Independent source review and lightweight checks

A read-only independent reviewer examined the final base-to-working source, Cunda fences, Improve flow, Services preservation, the constrained control rail, completing transport and inventory. No new concrete source/type/lifecycle/merge defect was found. The review explicitly left native typechecking, narrow-screen hit targets, actual capture timing and frame-admission/retirement/playback/failure proof pending.

Passed on this source:

- `git diff --check`; Services unchanged from base.
- Swift frontend syntax parsing on all 11 changed Swift files; this is not typechecking or an app build.
- Scoped Ruff and formatting for the lifecycle registry.
- Existing parity extractor: all 298 public actions mapped, no missing/stale entries.
- Journey mapping and complete UI inventory; no existing case removed.
- UI-selection Python suite: 26 passed, 13.086 seconds. These are selection/inventory tests, not native tests.
- Repository instruction-size guard: 37,982 characters, below the 38,000-character limit.

## Bounded next native slot — still not granted

Prepared runner: `.dev/editor-ai-halo/native-slot.sh`; preflight: `.dev/editor-ai-halo/simulator-preflight.py`. Both were syntax-checked only. They must not execute before parent allocation.

The preflight queries current installed simulator runtimes/device types at execution time. An existing receipt must name an available compatible device/runtime; a stale/missing ID stops the run instead of adopting another task's simulator. With no receipt, the supported helper may create a fresh owned compatible simulator. A second check after launch verifies that the new ID actually exists and is booted; helper doctor checks the source fingerprint and build receipt. Evidence is scoped to the actual run SHA.

One build, serial selection with exact result-count checks and no hidden retry:

1. 14 state/session tests: `NativeEditorAIActivityTests`, `NativeEditorPreviewHandoffTests`, `NativeEditorPreviewWorkSessionTests`.
2. One completing normal lifecycle UI case, recorded from the owned simulator into an original MP4.
3. Seven UI cases: completing reduced-motion and opaque captures; rejected prompt; repeated prompts/backgrounding; Improve/activity overlap; existing shared suggestion-set/Improve tap flow; existing conversation-sheet return.

Each test has a 180-second execution cap. Result bundles, failed attempts, summaries and attachments are retained. Only the owned recorder PID is stopped. Verify actual movie metadata, source SHA/build number/fingerprint, test counts, real pixels and motion before using Library's supported batch upload. No mockup or reference clip becomes an after.

Actual changed-document cached/uncached retention, paused/playing and superseding readiness/retirement, same-item newly composed-frame admission, retained failure/retry and export negatives remain correctness gates after initial fixture viability. Do not publish or admin-merge an untested candidate. No native/package build, native test, simulator launch/install, phone/device control or Library capture upload ran during this integration.
