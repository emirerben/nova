# KRI-1230 candidate on landed main

Tracking: [KRI-1230 — Show AI editing activity in the iPhone editor](https://linear.app/kria/issue/KRI-1230/show-ai-editing-activity-in-the-iphone-editor).

## Exact integration

- Initial fetched base: `c078f13b9ad6f0f60d4a5e94f4cd2afac551a76b`, including Cunda merge `70bff5f3738ca8f6873e3a85ddd9fbad374ada49` and #2109 Improve/suggestion cards. Current source-only integration is described below.
- Branch/worktree: `codex/editor-ai-halo-main`, `nova-editor-ai-halo-main`.
- Original source/layout/capture-test commit: `4bc31a31f66735031790daa126b1dc1cf0188a9e`; first native run at `d0c89bb6866da83503b3630de8e0922457c41de3`. The subsequent helper correction changes UI tests only and needs a newly allocated native run.
- Preserved comparison candidates: standalone `d8c93aa545609c1d272986d623645885487d255e`, prior integration `990f169fd0b0095b482585c149b82c7d3fdedc29`.
- Only the six halo commits after the old Cunda integration base were transplanted. No Cunda implementation/test/release commits were replayed. Current repository instructions and applicable native/motion/evidence guidance were inspected.

## Conflict and overlap resolutions

The two textual conflicts were adjacent declarations and initializer assignments in `NativeEditorView`: keep both `improve` and `conversationActivity`. The chat call keeps `improve: readyCardsPresentation` and adds only the activity projection. `Services.swift` is byte-for-byte unchanged from the fetched base.

The retained Improve sheet, checked-card tap, dismissal-then-conversation flow and suggestion ledger remain in charge of their original state. Cunda's load epoch, guarded editor-open fallback, dirty-state/retry UI, first-open seed admission and target/generation checks remain intact.

Two independent bottom-corner overlays could overlap once the halo button expands. They now share a width-constrained bottom HStack with Improve given layout priority and the activity label allowed to wrap. Button identifiers/actions, corner padding and sheet behavior stay the same. Halo animation also pauses while Improve is presented. A UI regression checks separated, on-screen, hittable controls during a real fixture POST and verifies Improve still opens.

## Completing actual-app capture cases

Added `testEditorAIActivityCompletingLifecycleCapture`, `testEditorAIActivityCompletingReducedMotionCapture` and `testEditorAIActivityCompletingOpaqueCapture`. They use identical seeded project/media/prompt and the existing offline HTTP fixture. A distinct `completingFollowup` option selects `KRIA_CHAT_THOUGHTS=1`; the transport delivers the correlated terminal response after six delta ticks. The existing held-turn option still selects `hold` for its own inspection case.

Each completing case is intended to attach real app idle, sending, working and settled screenshots only after assertions, checking preview/play availability and absence of a previous-preview notice at settlement. The first normal run failed before sending; reduced-motion and opaque captures remain unrun. Its real screenshot/movie show initial preview preparation, not a completed prompt lifecycle. Historical installed build-1902 before remains separate and is not pixel-matched to the simulator fixture. These captures do not establish a real changed-document handoff or live-model/export result.

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

## First allocated native slot

Runner: `.dev/editor-ai-halo/native-slot.sh`; preflight: `.dev/editor-ai-halo/simulator-preflight.py`. The parent allocated one bounded batch, executed at `d0c89bb6866da83503b3630de8e0922457c41de3`. Another native run requires a new allocation.

The preflight queries current installed simulator runtimes/device types at execution time. An existing receipt must name an available compatible device/runtime; a stale/missing ID stops the run instead of adopting another task's simulator. With no receipt, the supported helper may create a fresh owned compatible simulator. A second check after launch verifies that the new ID actually exists and is booted; helper doctor checks the source fingerprint and build receipt. Evidence is scoped to the actual run SHA.

One build, serial selection with exact result-count checks and no hidden retry:

1. 14 state/session tests: `NativeEditorAIActivityTests`, `NativeEditorPreviewHandoffTests`, `NativeEditorPreviewWorkSessionTests`.
2. One completing normal lifecycle UI case, recorded from the owned simulator into an original MP4.
3. Seven UI cases: completing reduced-motion and opaque captures; rejected prompt; repeated prompts/backgrounding; Improve/activity overlap; existing shared suggestion-set/Improve tap flow; existing conversation-sheet return.

Each test has a 180-second execution cap. Result bundles, failed attempts, summaries and attachments are retained. Only the owned recorder PID is stopped. Verify actual movie metadata, source SHA/build number/fingerprint, test counts, real pixels and motion before using Library's supported batch upload. No mockup or reference clip becomes an after.

### Executed outcome, 2026-11-06

Fresh preflight found no competing native work, about 317 GiB free, and available iOS 26.5/iPhone 17 support. The helper created simulator `67B490F2-1A52-4A06-9B3B-201E897BF07A`; no existing simulator was adopted. One build-for-testing passed, as did both doctor checks. Actual bundle: `com.kria.app.dev`, version `0.1.0`, local build `1`; binary SHA-256 `3c20b8c2c20e090e786a17f3e150a108448fce540791406567e506c78bddccfc`. Generated-project input fingerprint: `b2971d525b144ebf852ba0ca42fc34fe59e75bd3e9c7ee9fdf7afa1e95333bce`. This includes ignored generated project/scheme inputs absent from the earlier preparation fingerprint.

The 14 selected state/session tests passed, with zero failures or skips. The normal lifecycle UI case failed (one failed case, three assertion/action failures) before any prompt was submitted. It asserted idle about two seconds after opening while real preview preparation was still active, then scoped the composer lookup under the conversation-history element even though the actual composer/send controls are siblings. Seven remaining UI cases never ran. Batch exit: `1`.

Evidence remains under `.dev/editor-ai-halo/native/d0c89bb6866da83503b3630de8e0922457c41de3/bounded.T5tZiX/`: state/normal result bundles, summaries, attachments, original recording and source/build manifest. Actual pixels were inspected; the screenshot labeled idle by the failed test is preparation, not idle. The unmodified XCUITest movie is a diagnostic of the failed attempt, not proof of prompt-driven completion or accessibility variants.

The source-only correction waits for and requires genuine initial idle, stops the caller on timeout, queries the reachable composer/send by their actual labels without falling back to the covered chat, requires Send to be enabled, and dismisses using the actual Sheet Grabber. Rejected-draft verification reads that same reachable composer. An independent reviewer accepted this repair without finding a weakened assertion. Swift syntax parsing and whitespace checks passed; the corrected helpers have not run natively.

The recorder stopped and the owned simulator was shut down/deleted. At **14:32:31 UTC**, no native build/test/recording processes remained and about 310 GiB was free. The lane was explicitly released. No second heavy batch, physical phone action, paid call, backend mutation, publication or merge ran.

Actual changed-document cached/uncached retention, paused/playing and superseding readiness/retirement, same-item newly composed-frame admission, retained failure/retry and export negatives remain correctness gates after initial fixture viability. Do not publish or admin-merge this candidate until the corrected native checks and remaining required proof pass.

## Source review after shipping continuation

Freshly fetched main is `dbb260e47ef640b8c0908808df640723e0f8c1a2`, adding #2120 Talk to Camera instrumentation. The isolated halo branch rebased without conflicts to `76490efa8a0b87eddbbcd93fa287c13d376d56c3`; preserved branch `codex/editor-ai-halo-pre-bench-4fcc` retains the earlier helper head. Services, DeviceRendering and upstream media instrumentation remain intact.

Two independent source reviews identified and re-reviewed repairs committed in **`08c20c87f87eb7f086cacaaae52d412c4deafb14`**:

- A paused scrub image could cover a player at an older time. Retention now keeps the existing owned displayed image/time and presents it in both editor and fullscreen. It retains one image reference; it does not capture a new frame for decoration.
- Changing only the work ticket previously reused an already-ready layer to acknowledge an in-place edit. Paused work now waits for its matching generated image to be published. Playing work needs both a current-revision frame receipt and layer readiness, in either order. The one-shot receipt belongs to the player item's compositor instance, is armed before immutable instruction publication, and finishes only for a successful whole-frame render. Auxiliary image generators and sliced instructions cannot acknowledge playback. No polling, additional render loop or pixel-buffer retention was added. Compositor availability/instance continuity still needs native proof.
- Full preparation and generation-rebase preparation paused before preserving playback intent. Both now preserve owner/control-epoch-scoped intent before their internal pause. User pause/play/seek and reset invalidate it; private internal positioning and duration clamps preserve it. Newer user controls win over automatic resume.

The existing cached in-place update, audio guards, recipe/export snapshot and current/previous export-authority checks remain. Receipt cancellation, paused-image failure, superseding work, dismissal and target/lifetime reset use the existing guarded terminal paths. A produced compositor frame is not proof of physical display; native inspection remains mandatory.

Three new `NativeEditorPlaybackFrameReceiptTests` were written before the receipt implementation: wrong revision/separate compositor instance, replacement/cancellation/duplicate completion, and reentrant acknowledgment outside the lock. The cancellation control completes the exact cancelled revision. These tests have **not run**. The source failure matrix is retained locally for the next allocated run.

Both reviewers accepted the corrected source with no remaining concrete blocker. Lightweight checks passed: syntax parsing of all 14 changed Swift files (not typechecking), existing parity extraction (298 actions), complete UI inventory (410 cases), existing unmapped-journey check, valid JSON, whitespace and the instruction-size guard. No native build, test, simulator, package resolution or recording ran during this continuation.

### Proposed next bounded slot

Only after explicit parent allocation, fresh native-process/disk/simulator checks, and any required integration with speed's overlapping engine changes:

1. One isolated exact-head build and doctor. Require `KRIA_HALO_EXPECTED_HEAD` to match the reviewed final head.
2. **28 state/receipt/image-request tests:** the original 14, three new playback-receipt tests, and the 11 existing `NativePreviewImageRequestTests` cancellation/coalescing/old-generation controls.
3. The same **eight UI cases**: completing normal, reduced-motion and opaque captures; rejected prompt; repeated/background requests; Improve/activity coexistence; shared suggestion/Improve flow; conversation-sheet return.
4. Preserve all failed results and real source/build-bound captures, inspect actual pixels/motion, and clean up only the owned recorder/simulator on every exit. No automatic retry.

The local command is `KRIA_HALO_EXPECTED_HEAD=<reviewed-final-head> bash .dev/editor-ai-halo/native-slot.sh`. It is prepared, not executed. Native typechecking, actual compositor-instance delivery, paused/playing changed-document handoff, stale callback interruption, manual edits/Undo, retained failure/retry and export-current refusal remain open. The no-op completing capture alone cannot satisfy those changed-content gates.
