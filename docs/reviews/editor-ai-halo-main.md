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

## Integration with current landed fixes

Current pinned main is `f429e2a0c840e8eeba3d55c233e016d59882b8d4`, including speed #2125, re-record #2123, automatic result recovery #2137, explicit-pause #2024 and reset ownership #2134. The prior halo candidate remains on `codex/editor-ai-halo-pre-current-a243`. Only halo commits were rebased; unpublished candidates were not stacked.

The four conflicted files were resolved by preserving both contracts: lifecycle parity keeps `setObserversVisible` and the halo callbacks; editor-load cleanup keeps automatic-result drain/observation/save-history resets and halo admission/work resets; export refusal for a retained previous preview remains outside speed's local-file fast paths; view disappearance ends observers and preview presentation; the UI catalog contains both re-record and halo cases. Services and DeviceRendering remain unchanged from main. Engine receipt hooks remain isolated from the landed speed changes.

Independent review found one deferred-conflict integration defect: the initial prompt-sync scope retires its retained preview when a conflict pauses mutation, so the later choice must acquire a fresh retention record. Source repair **`ea2f47b0ec673831b9757e71607a93858299cbe3`** retains immediately before resolved merge mutation only for matching job/item/variant, and retires the record if no preview sequence advances. Cross-target retention remains excluded.

`testDeferredSameTargetConflictRetainsItsAdmittedPreviewWhenResolved` was added before the repair. It reuses the existing overlapping-trim/generation conflict journey, prepares/adopts a fixture source preview, resolves the deferred choice and requires previous-preview status `.updating`. The manual surface-boundary callback is a state control, not physical-frame proof. The new test has not run.

Both source reviewers accepted the integration and corrective fix. Passed lightweight checks: syntax of 15 changed Swift files, 300 mapped public actions with no drift, complete 417-case UI inventory with no upstream cases removed, unmapped-journey check, whitespace and the 37,999-character instruction-size guard. No native build/test/simulator/package/recording ran in this continuation.

The proposed next batch adds the deferred-conflict regression plus five existing integration controls (explicit-pause KVO, ordinary pause/resume, pause during scrub handoff, local saved-render routing before/after publication, server-verification refusal) to the prior 28, for **34 state/receipt/image/integration cases and the same eight UI cases** after one build. This proposal is not a lane allocation. The combined-main iOS compile fix has priority; this task owns no native process. Publication and merge remain blocked until current-head native verification and the remaining changed-frame/export gates are satisfied.

## Allocated validation and fixture repair — 2026-11-06

The exact tested source was `2fdc1d4f0a867d9389dec9f58f0d20a3af10fa5f`, based on freshly fetched main `e689040d6719a99d4805da2cf6117d60aea8e263`. It carried only three test-target compilation witnesses from the independently compiled combined-main repair; no runtime repair, assertion change or unpublished commit ancestry was imported. The isolated build-for-testing passed, including the new media-engine receipt hooks.

The bounded selection executed **34 state tests: 33 passed, one failed, zero skipped**, and **eight UI cases: four passed, four failed, zero skipped**. Normal, reduced-motion and opaque completing lifecycle captures passed, as did rejected-prompt draft recovery. The failed state case was `testDeferredSameTargetConflictRetainsItsAdmittedPreviewWhenResolved`: its spy lacked the source-pool response used by normal asynchronous preparation. Its local preparation shortcut raced that real path, producing the unsupported-API failure before admission.

The failed UI cases were repeated prompts/background return, Improve/activity coexistence, conversation-sheet return and shared suggestion cards. Repeated prompts reached initial idle and sending; the first failure was post-send settlement. Exported hierarchies prove fixture contamination: a rejected draft was concatenated with the next prompt, earlier capture prompts remained in its transcript, and subsequent cases retained that same direction-approval screen instead of a ready editor. Explicit `-perf-reset` cleanup previously ran after the in-memory container's early return, and did not clear seeded submission/draft keys. The later tests did not reach their intended editor assertions.

The source-only correction supplies genuine local montage sources under a unique project, pins the pool to `g1`, `g2` and `g3` at their actual handoffs, and waits for normal bounded preview readiness. It preserves the conflict, generation, dirty-state and admitted-preview assertions; no global spy default changed. Explicit DEBUG fixture reset now clears only the fixture account's transcripts and exact seeded thread draft/submission entries before AppModel construction. Warm launches and unrelated accounts/receipts are preserved. Existing disk-only media/poster reset remains disk-only. The repeated/background test uses the correlated completing followup and retains its failure assertions while stopping on a failed prerequisite. Both reviewers accepted these corrections; syntax and whitespace checks passed. **The corrected source has not been compiled or run.**

Actual inspected captures from the passing normal case show idle, sending and working, plus a separate settled screenshot. The shortened movie contains source seconds 16–42; it ends during working and does not claim to show completion. Its Library ID is `libfile_7c2d28a671988191a16e6692de62bb0e`. Stills: idle `libfile_edbbb8a3888481918e458f9a97f016ac`, working `libfile_98f9b9ab64fc8191bbfafca4fe8e2ed9`, settled `libfile_cce8f911bc6081918699d8e08c74d811`, reduced-motion working `libfile_6029afdd116081918718c2cbdadcc90a`, opaque working `libfile_4df53738b1fc8191b68c847efc137469`. All are simulator fixture output at the tested SHA, app 0.1.0 build 1. They are not changed-document/export fulfillment proof or a pixel-matched comparison with historical phone build 1902. The older initial-preparation artifacts remain separately labeled.

The owned simulator was cleaned up and the lane released at **23:04:43 UTC**, batch exit 1. No owned native build/test/app/recording process remained; free disk was 308,012,736,512 bytes (about 287 GiB). The pre-existing Simulator application was untouched. No second native batch was launched. A new exact-head build and the corrected 34+8 selection need parent allocation after the queued combined-main repair; actual changed-frame/compositor, clock, retained failure/retry, manual-edit/Undo and previous-preview export-refusal proof also remain required before publication or merge.

## Corrected-fixture rerun and remaining proof — 2026-11-07

Fresh main `b9177a6a462819f33bdae1995cdc348eb05b3b2f` added backend/release changes only. The corrected candidate was preserved, then rebased without conflicts to **`f66b294955976362dc47438c2db7ac373692b450`**; its iOS inputs matched the prior candidate. The allocated build and doctor passed. Actual selection: **33/34 state tests and 7/8 UI cases passed, zero skipped**. Repeated prompts/background return, conversation-sheet return and shared suggestions now passed after the isolation correction. Normal, reduced-motion, opaque and rejection cases passed again.

The remaining state failure reached actual source readiness and retained presentation. The final equality compared cached raw slot dictionaries: the typed local trim stayed at 1.2 seconds while merge encoding correctly canonicalized an old raw 1.5-second value and added null/default fields. The correction compares the complete independently captured authored timeline snapshot and explicitly checks asset identity, clip index, start and trim. It keeps the two original helper callers' exact whole-slot assertion. No unknown fields are deleted. Export-route and source-export refusal are now checked after the real replacement composition/item is ready, while no mounted surface has admitted it and the previous picture remains presented.

The remaining UI failure expected Improve while a message was sending. Main intentionally removes ready suggestions for pending messages, thinking and actions. The corrected case keeps that policy and tests actual preparation alongside ready suggestions, delaying only the source-pool response. It requires expanded-control/Improve nonoverlap, viewport containment, hitability, sheet open/dismiss and continued availability after actual preparation ends.

The batch exited 1 and released its lane at **00:22:38 UTC**, with no owned native processes and 311,761,080,320 bytes free (about 290 GiB). Its source/build manifest, result bundles and actual captures remain under `.dev/editor-ai-halo/native/f66b294955976362dc47438c2db7ac373692b450/bounded.H4i7TU/`. The inspected normal working screenshot retains the actual video under the palette halo. Existing Library IDs retain their original tested revisions and scope; no new implementation capture was fabricated or relabeled.

Three additional proof controls are prepared in source, not executed: a real mounted `NativeVideoPreview`/AVPlayerLayer test requires advancing playback, changed composition/ticket and settlement through the actual compositor while retaining player/item/compositor identities; a source-pool 503/retry control requires terminal failure status, stopped activity and preserved full timeline before real g4 retry; and an opt-in changed-title UI journey captures old/retained/current pictures, blocked export actions and normal manual edit/Undo. The changed generation remains `rendering` without a fabricated output URL. Its native source admission is checked separately from unfinished encoded rendering. No session readiness or admission override was added to that UI fixture.

Both reviewers accepted the corrections and new proof interfaces from source. Syntax, whitespace, catalog and instruction-size checks passed; these are not compilation/runtime proof. The prepared next allocation is one build, **36 state/session cases and nine UI cases**, no automatic retry, with real pixel inspection required for the changed-title captures. Playing generation-handoff/resume, interruption and encoded-export/device fidelity remain separate proof limits. Publication and admin merge remain held for final-head verification and the parent's readiness gate.

The editor verification-map entry now names these controls and their pending status. Index/source review was performed; a full maintenance pass driving every mapped feature was not run within the explicitly bounded native allocation.

## Native handoff inspection and corrections — 2026-11-07

At exact source **`017f2a5e665ac560ebcd12bbfab4c4969a268e4c`**, the allocated build and doctor passed. **35/36 state/session tests and 8/9 UI cases passed, zero skipped.** The actual playing-surface/compositor case passed in 0.467 seconds, retaining player/item/compositor identities while admitting the new revision and advancing playback. The full canonical timeline and ready-candidate export-refusal case passed. The changed-title native UI journey passed in 64.738 seconds, including blocked export actions and manual edit/Undo. All other UI cases passed except Improve's Done locator.

The source-failure/retry fixture timed out before observing its injected 503. Source review found unstable fixture identities: non-UUID slot IDs without explicit asset IDs synthesize fresh UUIDs on authoritative decode, which can cause a second dependent merge conflict before the source request. The exact old stopping guard was not logged. The retained fixture now pins the same UUID slot identity and the actual imported asset ID across generations, preserving all other fields and both original callers. Before waiting for failure, it requires g4 adoption, no conflict, and an increased source-pool request count; failure messages include generation, conflict, source state and calls. No remote text-timing change was added to evade the atomic merge rules.

Improve's layout and sheet-open assertions passed. Its Done button was visibly present but its identifier was `improve-sheet`, inherited from the container. The harness now selects the exact visible, hittable Done label; dismissal, genuine idle and reopen assertions remain.

Actual old/retained/current/manual/Undo screenshots were inspected. They show the expected OLD title, NEW title, manual note and restored NEW title, but the retained picture visibly shrank within black borders and its previous-version notice sat beneath the bottom activity capsule. This is a visual failure despite the UI case passing. The view-only correction removes the layout-consuming picture/status stack: the retained image/player keeps the full canvas, while status and Retry use an opaque top overlay. Candidate readiness, player ownership, fullscreen handling and passive-canvas guards are unchanged. The UI case now checks stable preview bounds and notice containment/nonoverlap, and deselects manual text before its capture. New captures must confirm actual picture scale; container geometry alone cannot prove it.

The lane released at **01:42:44 UTC**, batch exit 1, with no owned native jobs and 295,668,465,664 bytes free (about 275 GiB). Evidence remains under `.dev/editor-ai-halo/native/017f2a5e665ac560ebcd12bbfab4c4969a268e4c/bounded.jTjAaG/`. No follow-on native batch was launched. Fresh main `a518aed8c86f6ed9a17004327141f252eb395502` adds backend-test/release changes without overlapping these corrections.

Both source reviewers accepted the fixture, locator, presentation and strengthened-check changes. Swift syntax and whitespace checks passed; corrected runtime/pixel proof remains pending. The next prepared scope remains one build, **36 state/session and nine UI cases**, without automatic retries. No push, PR, admin merge, deployment or phone action has occurred.

## Corrected native and visual verification — 2026-11-07

The allocated batch at exact source **`9a2b7203328ee7b3c1fd026c8ec08a61b635f4c4`**, based on **`a518aed8c86f6ed9a17004327141f252eb395502`**, passed build-for-testing, both doctor checks, **36/36 state/session tests and 9/9 UI cases**, with zero failures or skips. The real playing compositor/surface admission case passed in 0.429 seconds; source failure/retry passed in 0.426 seconds; changed-title/manual-edit/Undo passed in 67.797 seconds. Normal, reduced-motion, opaque, rejected-prompt, repeated/background, Improve preparation, shared suggestions and sheet-return flows all passed. No retries were used.

Actual simulator screenshots were inspected separately from the UI assertions. The OLD picture remains at its original full size during the handoff; the previous-version notice is readable above the activity control; the admitted picture visibly contains the NEW title; a deselected manual note appears fully and Undo removes it while preserving the NEW title. This corrects the failed visual acceptance from `017f2a5e`. The source-preview fixture deliberately leaves generation 2's encoded render pending, so these pictures do not establish encoded-export fidelity or deployment.

The run used an isolated iPhone 17 simulator on iOS 26.5, app `com.kria.app.dev` version 0.1.0/build 1. Evidence is retained under `.dev/editor-ai-halo/native/9a2b7203328ee7b3c1fd026c8ec08a61b635f4c4/bounded.RnKr5A/`: three xcresult bundles and summaries, actual captures, source/build manifest and visual-inspection record. Built app SHA-256: `57bfcb1edce8770551245cc5a97f68b31d53bb1a2071c130aaeb2e3fc63b748e`; build-input fingerprint: `9284cd3dfd64bdcbc84bd26c55b523170c6090c3718c16bcb7d983ab4c8e3e32`. The batch exited 0 and its owned simulator/recorder were cleaned up. A fresh process/disk check released the lane at **02:45:32 UTC**, with no native runners and 291,904,569,344 bytes free.

Upstream main was fetched again after the run and remained the tested base. Independent final source review found no supported defect. Subsequent verification-note updates change documentation only; runtime/test/build inputs remain those executed at `9a2b7203`. Physical-device interruptions, playing cross-generation handoffs and encoded-export fidelity remain outside this bounded proof. No deployment or TestFlight release is claimed.

## Recorded-motion correction — merge held

Further inspection of the actual `9a2b7203` normal recording exposed a motion defect that the aligned stills missed. At 24 seconds the keyboard is dismissing; at 26–29 seconds the halo sits above and then crosses the activity control; by 35 seconds it aligns. The eight-second `PhaseAnimator` transaction around the whole decorative subtree interpolates its changed layout independently of the control. Independent review inspected the recorded frames and confirmed the defect. The build and 36/9 test counts remain valid historical results, but motion acceptance is **failed**, superseding any implication that the animation itself was fully accepted.

PR [#2145](https://github.com/emirerben/nova/pull/2145) was published at `408cda2d9df3cf82b72c178e4afd65fb0420a442` and returned to draft; merge is held. The narrow local correction replaces `PhaseAnimator` with a conditional `TimelineView` that derives only the decorative gradient angle from an eight-second cycle, and disables implicit animation on that subtree. Its requested minimum interval is 1/30 second; actual cadence is system-controlled. Request/preview ownership, foreground gating, static accessibility treatment and all media/export behavior are unchanged. This is decorative motion, not progress estimation.

The corrected source needs a new parent-allocated native run and continuous inspection through keyboard dismissal, sheet return and status changes. No follow-on native batch has been launched. Reuse the existing actual recording as the negative visual control; aligned screenshots and accessibility geometry alone are insufficient. The already uploaded OLD/retained/NEW/manual/Undo stills remain truthful handoff evidence at `9a2b7203`, not proof that its continuous motion passed.

## Corrected-motion build checkpoint — 2026-11-07

The next allocation ran exact `4b1538c67c86ed2027f659e53b2bbcc8c16c081d` against freshly fetched main `a518aed8c86f6ed9a17004327141f252eb395502`. Build-for-testing exited 65 before any state test, UI case or capture ran. The app's existing `TimelineView` in `EditorViews.swift` shadowed the intended SwiftUI type; the call is now explicitly `SwiftUI.TimelineView`. Syntax parsing alone did not detect the name-resolution error. The owned simulator was cleaned up and a fresh process check released the lane at **03:28:47 UTC**, with no native runners and 289,724,997,632 bytes free. No second heavy batch was started.

Hosted validation of published `408cda2d9` also completed: 20 checks passed, and the iPhone unit compile plus its three dependent aggregate gates failed. [CI run 42331587028](https://github.com/emirerben/nova/actions/runs/42331587028) reports `SlidePostChatEditTests.swift:16:40: sending 'self.defaults' risks causing data races` in synchronous teardown on Xcode 26.3. The source correction uses async XCTest setup/teardown in that fixture and the matching `SlidePostTests` fixture, following the existing documented pattern in `SlidePostSessionAutoAppendTests`. It preserves main-actor isolation, every assertion and the complete cleanup path; no checks are disabled. The newly allocated batch had failed in app compilation before reaching these test-target files, so their corrected compile/runtime status is still pending.

The namespace and test-fixture corrections passed Swift syntax and whitespace checks. They require a newly allocated build/test/capture run and hosted checks before publication or merge. PR #2145 remains draft at its historical tested source; no corrected-motion recording exists yet.

## Corrected motion verified — 2026-11-07

The next authorized batch at exact source **`e11db5a7ced4cd1048c149b7fc47f431a890e2a3`**, based on unchanged main **`a518aed8c86f6ed9a17004327141f252eb395502`**, passed build-for-testing, both doctor checks, **36/36 state/session and 9/9 UI tests**, with zero failures or skips. This build includes the qualified SwiftUI type and both async test lifecycle corrections. It does not establish compatibility with the hosted Xcode 26.3 toolchain until its separate CI run passes.

The actual normal recording was inspected with temporal samples around keyboard/sheet dismissal, editor expansion, Sending-to-Working and settlement. Root samples at 24, 26, 29, 31, 35 and 41 seconds and independent samples at 27, 27.5, 28, 28.5, 29, 35, 41, 43, 44 and 45 seconds show the halo attached to its control; by 45 seconds the expanded capsule/halo is gone and the sparkle remains. The earlier `9a2b7203` frames at 26–31 seconds remain the observed negative visual control. This is sampled inspection of recorded motion, not a claim of frame-by-frame playback. Later SpringBoard frames are excluded from presentation.

A stream-copy excerpt from the actual recording is retained in Library as **`libfile_cbed66fd87688191a6b11e1b0b23e65e`**, `kria-editor-ai-halo-e11db5a7.mp4`, 21.060 seconds. It includes keyboard/sheet dismissal, Sending, Working and settled editor, without recreating motion or fabricating a changed render. Actual current-source handoff stills separately show the full-size retained OLD picture/readable notice, admitted NEW title, deselected manual note and Undo. Reduced-motion/opaque cases and interrupted/rejected/Improve/navigation cases passed their selected UI checks.

Evidence remains under `.dev/editor-ai-halo/native/e11db5a7ced4cd1048c149b7fc47f431a890e2a3/bounded.aBZqan/`: result bundles/summaries, original recording, excerpt, source/build manifest, motion inspection and lane receipt. App version 0.1.0/build 1; binary SHA-256 `c19f8a7ddd6daf1745e0c8e951f9f679db20465001b555b1bb38f0e199e3bca7`; build-input fingerprint `896fb32b3789e57280c92e6e4530fa0ecc6ebc8af05ab45706d32326dc1d1ee6`. The batch exited 0, cleaned its owned simulator/recorder, and released the lane at **03:43:15 UTC** with no native runners and 285,754,294,272 bytes free. Subsequent result-note updates are documentation only; no extra native batch is implied. Hosted verification and landing remain separate gates; physical-device, playing cross-generation, encoded-export and production limits remain unchanged.

## Hosted failures and foreground-proof correction — 2026-11-07

PR #2145 is draft again; merge is held. The published head is `9f0150bab3e176807195cc6dc8e7ce8ab61200bc`, based on unchanged main `a518aed8c86f6ed9a17004327141f252eb395502`. The available [hosted iPhone log](https://github.com/emirerben/nova/actions/runs/42335679019/job/128292072244) contains **25 failed cases** through 04:19:18 UTC: 13 ChatDraftStaging, three DeviceRender, one halo playback proof and eight NativeEditorSession cases. This is a partial log, not the completed job's total. A pending job conclusion must not be described as an absence of failing cases.

Independent source comparison found no supported halo cause for the 24 ChatDraftStaging/DeviceRender/NativeEditorSession failures. Their relevant adoption, merge, consent, retry and fixture paths are unchanged from the base. The concrete findings are asynchronous adoption assertions without settled authoritative fixtures; unstable slot/asset identities causing dependent-content conflicts; assertions of older unconditional prompt/retry policies; a queued-resume fixture missing the matching render generation; retry-hint sanitization/live-clock expectations; and observation of a successor before cancellation cleanup finishes. These are source-path equivalence findings, **not an executed baseline reproduction**. The separately coordinated combined-main repair is still held for approval and has not been copied, stacked or published here.

The branch-specific playback case failed in 0.025 seconds at the foreground `UIWindowScene` unwrap, before any preview work. It had passed locally after the launch helper foregrounded the app. Its replacement uses a DEBUG-only app fixture and XCUITest-owned cold launch/activation, with the real `NativeVideoPreview`, player and compositor. The short playback/edit sequence stays inside the app process so cross-process assertion latency does not consume the four-second source. Duration, initial admission, advancing clock/rate, early edit, immediate changed text/pending/playing state, new composition/ticket, retained player/item/compositor identities, ready state, absent previous preview and final text/time assertions are preserved. No readiness callback, skip or synthetic progress was introduced. The old foreground-dependent unit method alone is removed; the replacement is required in both editor and smoke selection and remains journey-mapped.

Source review, Swift syntax parsing, whitespace and UI inventory checks are the current evidence for this correction; they do not establish compilation or execution. The prepared next allocation is a build, **35 state/session cases and ten UI cases**, including this proof as the first cold-app UI case, with actual diagnostics and screenshot retained. No native command has run since the 03:43:15 lane release. After the approved baseline repair lands, integration and affected validation must run on the actual resulting main before push/readiness/merge. Verification-map index/source maintenance is scoped to the editor; a full live map-maintenance pass remains unrun under the native-lane hold.
