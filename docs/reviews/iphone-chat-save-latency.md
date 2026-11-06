# iPhone latency: New chat, Save, export after Save

**Current integration result:** pinned main `1e91cb694165e6a0a0793cff21de6c74a3107918`, merge `817ce18385a7d6bbb1bcbf1116f9aaec9dfcc9b4`, tested code/UI fixture `4ffa7aa49ac76038ea0603992a75fb5a21864203`. Post-integration native unit aggregate: 2584 tests, 27 skipped, zero failures. One delayed-New-Chat UI test passed. Formal default: exit 3, PASS with drift. See the final integration section; it supersedes earlier pending-composer UI and later-main-integration gaps. Physical-device timing, media fidelity inspection, tracker ownership and full model correspondence remain open.

Branch `codex/iphone-chat-save-latency` (worktree `/Users/emirerben/Documents/Codex/2026-11-05/task/nova-iphone-latency`), based on `origin/main` `56e21de1ab785c99f30cd09e5c3e2d7466240af2` (includes #2105 Steer now and #2118). **Tested code commit: `4cc324f6c0b0d3bd8561ad87ae07e26e1a23d7d6`** (the commit after it only adds this report). Local only: not pushed, no PR, nothing deployed.

## Evidence level

**Offline and Simulator unit evidence only. No signed-in device, no live network, no live speedup claim.** All timings below labelled SYNTHETIC use an injected delay and prove call structure, not real latency. The existing explanation that rendering dominates "Save feels slow" remains **unmeasured**. Historical Linear numbers (KRI-1185 ~52 s, KRI-1184 369 s) were not reproduced and are not baselines.

## What was awaited (read from code)

- **New chat:** one `POST creation-threads` before anything appeared (KRI-640, Done, historical: #1607 had already removed the capabilities and read-back trips). The button was also disabled while the project list loaded (`isLoading`), so a tap during that read did nothing.
- **Save:** commit, then a committed-variant read (1-2 GETs, up to 450 ms of retry sleeps), then a fresh capabilities GET (the cache never read its stored entry), then the device-render status read, strictly one after another, before the phone render could start. The render itself is not awaited by Save.
- **Export after Save:** disabled until the render uploaded and the server published it, then a re-download of the video the phone itself rendered (`applyPreviewVariant` swaps the phone file for the server URL).

## Implementation

| Area | Change |
| --- | --- |
| New chat (`AppState.swift`, `BrandComponents.swift`, `ChatWorkspaceView.swift`) | `AppModel.startNewChat()` shows `PendingNewChat` synchronously; the create runs in the background. `PendingNewChatView` shows "Starting your chat…" with the real `ChatComposer`: **typing works**, send and attach are blocked (`blocksSubmission`, `canAttach: false`) until the server thread exists. Typed text lives only in the pending state, moves into the new chat's own `ChatDraftStore` entry on success, survives failure and "Try again", and is never written to another conversation. Failure shows retry and "Back to my chats". Choosing another chat or the camera roll while creating leaves the pending chat; the late reply does not steal navigation (the created chat is kept only if it holds text, else discarded). The view puts the pending chat ahead of the camera roll. Repeated taps are one create. The button no longer waits on the list load; `openWorkspace` no longer restores the remembered chat over one the creator opened, and the launch sweep skips chats made this launch. |
| Save (`NativeEditorSession.swift`) | The committed-variant read runs beside `refreshDeviceRender` (joined before the identity guard and `startPreviewRefresh`; cancellation-aware; identity-checked before any state write). `refreshDeviceRender` reuses a capabilities read under 60 s old (creator Retry reads fresh). |
| Export (`NativeEditorSession.swift`) | `savedEditDeviceRenderFile()`: Save to Photos / Share use the phone's own finished render of the saved edit, before and after publish. It requires no unsaved or staged change, not saving, non-empty editor, phase `localReady/syncing/synced`, no `requiresServerRetry`, the file present, the render request identity equal to the one this session's latest save pinned, **and** the document generation equal to that save's generation or the presentation's `publishedGeneration` (an adopted other-device generation therefore fences it). `DeviceRenderSessions` still withholds `localFile` while the server must verify the output. KRI-1032 canonical-render routing is otherwise unchanged. |

Not done on purpose: an immediate first preview-poll read (the commit read-back already read the variant; a 0 s read of a render that cannot be finished adds a call and no information). A warm pre-created thread was not built (the pending surface does not need it).

## Tests (Simulator `3F0D9C92-2BA8-4831-9AD6-F070EF630845`, task-owned, iOS 26.5, code commit `4cc324f6c`)

| Run | Result | Log |
| --- | --- | --- |
| Focused: `NativeEditorSessionTests` 266, `NativeEditorExportTests` 10, `ProjectActionsTests` 34, `ProjectRestorationTests` 2 | **312 executed, 0 failures** | `/tmp/kria-latency-test4.log` |
| Full `KriaTests` unit target | **2579 executed, 27 skipped, 0 failures** (447 s) | `/tmp/kria-latency-full-unit.log` |
| Mutation control: fence line removed | `testOwnRenderIsNotOfferedAfterANewerRemoteGenerationIsAdopted` failed as it should, then restored | `/tmp/kria-latency-mut.log` |
| `build-for-testing` | succeeded | `/tmp/kria-latency-build.log` |

New tests: New chat pending/typing/failure/retry/navigation/camera-roll/late completion/open-workspace/sweep (ProjectActionsTests); saved-edit file negatives for unsaved edit, missing file, newer remote generation, changed request, save in flight, output verification pending (file withheld) with an uploading control, needs-attention, deleted after a successful Save to Photos, concurrent download and Save; cancelled Save; commit-read overlap (NativeEditorSessionTests).

### Not run / pending (do not read as passed)

- **UI evidence that the pending composer is visible and typeable: PENDING.** No XCUITest for `pending-new-chat` exists. The parent interrupted the broad `CreationUITests`/`ProjectsUITests` run; it produced no result and is not a pass. The camera-roll case is covered at model level only (`showsCameraRoll` and `destination`), the view ordering (`pendingNewChat == nil` before the camera roll) is unexercised by a UI test.
- No signed-in device or live-network timing; no native export of a shared file compared frame by frame.

### Pre-existing flake: `NativeEditorExportTests.testCancelledNativeEncoderDoesNotRetry`

Fails at `NativeEditorExportTests.swift:58` ("Cancelled export must not restart playback"), the same assertion as hosted PR2108 `952487d` (run 42084295958 job 127472512109). Attribution, fresh process per run:

| Build | Fresh single runs | One process, 40 iterations |
| --- | --- | --- |
| Unchanged `origin/main` `56e21de1ab785c99f30cd09e5c3e2d7466240af2` (detached worktree, none of these changes) | 1 of 6 failed (run 1) | 0 of 40 failed |
| This change (`4cc324f6c`) | 1 of 6 failed (run 6) | 1 of 40 failed (iteration 1) |

Logs `/tmp/cancel-baseline-{1..6}.log`, `/tmp/cancel-baseline-iter.log`, `/tmp/cancel-changed-{1..6}.log`, `/tmp/kria-latency-cancel-changed.log`. The test uses a session with no device render, save or New chat, so it is unrelated to these files; it is an intermittent first-run failure on both builds. Not fixed (no evidence it belongs to this change).

## SYNTHETIC timing (injected delays; not a live measurement)

Same simulator, same fakes, only the delay injected. Work verified: counts are asserted, not assumed.

- **New chat**, create reply delayed 500 ms: changed build `testSyntheticNewChatSurfaceDoesNotWaitForTheInjectedCreateDelay`: pending conversation present after **0 ms**, real thread selected after **521-522 ms** (the delayed POST really ran). Unchanged main (`createProject`, temporary test in a throwaway worktree, not committed): first conversation surface after **529 ms**.
- **Save**, every awaited call delayed 300 ms (commit, variant read, capabilities read, device status read; each asserted to run exactly once): changed build `save()` returned after **919 ms**. Unchanged main running the same test: **1224 ms** (an actual run; the 1200 ms "serial" figure is the arithmetic 4 x 300 and matches it). The saving is the variant read overlapping the render's own reads. The spy has no capabilities cache, so the 60 s reuse (a further call saved in the real `KriaAPI`) is not in the number.

No live speedup follows: real network, server and render times differ, and the delays are uniform by construction.

## Remaining risks

- The 60 s capabilities reuse is a judgement call.
- If New chat is tapped before the first project list finishes, that launch's list stays on the cache until the next workspace open (the stale read is dropped by existing design).
- A file handed to Photos can in theory be removed by storage cleanup mid-write; Photos then reports an error through the existing export banner.
- The server side of `POST creation-threads` and `editor-commit` was not profiled.
- Preview poll cadence (first read after 1 s) is unchanged.

## Live / signed-in timing gaps to close before a speed claim

1. New chat tap to chat visible, cold launch, cached list, slow network.
2. Save tap to editor exit, interleaved before/after runs.
3. Share while the upload is pending shares the phone file; after publish no download occurs; the shared file matches the preview (KRI-1032 mutes, bars, labels).
4. UI test or manual check of the pending composer: type during the wait, failure, retry, back, camera-roll entry.

## Overlap

PR2105 and #2118 are in the base; the only shared file is `ChatWorkspaceView.swift`, with additive hunks outside Steer now (default-off and `only_if_queued` behavior untouched). PR2122 / Cunda source-window files (`editor_render_window.py`, `editor_empty_drafts.py`, `phone_rollout.py`, `test_editor_render_window_intent.py`, `docs/reviews/cunda-save-source-window-parity.md`) are untouched; nothing under `src/apps/api` changed.

## Gates

`scripts/preship-check.sh` on this branch: see "Preship" below. Decision trail: `.audit/iphone-chat-save-latency.tsv` (local, git-excluded). The poteto-mode cross-model trail review was skipped because the standing instruction forbids subagents.

## Files

`src/apps/ios/Kria/Core/AppState.swift`, `src/apps/ios/Kria/DesignSystem/BrandComponents.swift`, `src/apps/ios/Kria/Features/ChatWorkspaceView.swift`, `src/apps/ios/Kria/Features/NativeEditorSession.swift`, `src/apps/ios/Tests/KriaTests/NativeEditorSessionTests.swift`, `src/apps/ios/Tests/KriaTests/ProjectActionsTests.swift`, this report.

## Preship

Preship result is in "Review round 2" below.

## Review round 2 (three source-traced defects)

Base for this round: `19aa22d8b`; code commit for the fixes and tests: `06ab40c8078501a21e248b7a7ac45ebed5c4ded3` (the report commit follows it). The first-pass tested code commit `4cc324f6c` is superseded by it for the three areas below; `/tmp/kria-latency-test5.log` ran on the working tree identical to `06ab40c8078501a21e248b7a7ac45ebed5c4ded3`. Each defect was reproduced with a failing regression test before any fix (`/tmp/kria-latency-red.log`, build with the tests only):

| # | Defect | Red evidence | Fix |
| --- | --- | --- | --- |
| P1 | A failed device-status read after a second Save left the previous request R1 (still `synced`, file kept by `DeviceRenderSessions.wait`) as the "current" request; `saveOnce` pinned it to the new commit generation, so export handed over R1's video for the G2 edit | `testFailedStatusReadAfterANewSaveDoesNotLinkThePreviousRenderToIt`: `canDownloadCurrentVideo` was true and the route was `.localFile(R1 file)` | The own-render link now needs evidence from a SUCCESSFUL status read: the server's `base_generation` for the pinned request must equal the acknowledged save generation (`DeviceRenderSessions.baseGeneration(for:)`, `linkSavedDeviceRenderIfEvidenced()` at the end of every `refreshDeviceRender`, so a later healed read links lazily). A completed attempt is not evidence. The identity, generation, `requiresServerRetry`, verification-withheld `localFile` and unsaved-edit fences are unchanged. Control `testStatusReadThatNamesTheSavedGenerationLinksTheNewRender` (new request R2 with `base_generation` g2) passed before and after. |
| P2 | Typed text was lost when the creator left the pending chat (another chat or the camera roll) and the create then failed | `testTextTypedBeforeLeavingIsRecoverableWhenTheCreateFailsLater`: the next New chat started with an empty draft, for both targets | The failed create keeps the text as `AppModel.unsentNewChatDraft` without selecting or navigating anything; the next New chat starts with it, and it then moves to the new chat's own draft on success. Leaving a failed pending surface keeps it too; "Back to my chats" still discards. The previous chat's draft is asserted unchanged. |
| P3 | A Save with `renderStarted == false` awaited the committed-variant read and then wrote retry sections and `renderRetryNeeded` into whatever editor was loaded meanwhile | `testRenderNotStartedSaveCannotWriteRetryStateIntoAnEditorLoadedMeanwhile`: editor B ended as `renderRetryNeeded("Editor A's render did not start.")` | Identity guard after the await, as the render-started branch already had. |

Green: `/tmp/kria-latency-test5.log`, current build, simulator `3F0D9C92-2BA8-4831-9AD6-F070EF630845`:

| Class | Executed | Failures |
| --- | --- | --- |
| `NativeEditorSessionTests` | 269 | 0 |
| `ProjectActionsTests` | 35 | 0 |
| `ProjectRestorationTests` | 2 | 0 |
| `NativeEditorExportTests` | 10 | 1: the known `testCancelledNativeEncoderDoesNotRetry` baseline flake (line 58) |
| Aggregate | 316 | 1 (the flake) |

The cancellation flake: it failed in two class-level runs of `NativeEditorExportTests` in this round (`/tmp/kria-latency-test5.log`, `/tmp/kria-latency-export-rerun.log`) and passed 6 of 6 single fresh runs (`/tmp/cancel-changed2-{1..6}.log`). Unchanged `origin/main` `56e21de1ab785c99f30cd09e5c3e2d7466240af2` fails it too (1 of 6, see above); the test uses no save, device-render or New chat code. The earlier full-unit result (2579 executed, 27 skipped, 0 failures) predates this round and does not cover these changes; no broader run was made.

### Formal model drift (`formal/ios_edit_flow`, TLA+ render lifecycle)

TLC could not run here: the TLA+ jar download fails the checksum (`FAIL ... is not TLC v1.8.0`), so **no formal verification is claimed**. The offline drift part (function hashes against `coverage.json`, sync commit `b12bed356`) was recomputed: 26 covered functions already drift at unchanged `origin/main`; 28 drift now. The two NEW drifts are caused by this work: `NativeEditorSession.swift:saveOnce` and `:refreshDeviceRender` (`retryDeviceRender` also changed but already drifted at baseline). The model was not re-read or updated against them; that review is open.

### Tracker and evidence metadata

No valid tracker issue is assigned. KRI-672 is research backlog and KRI-640 is the historical, Done New chat delay; neither owns the unfinished fulfillment and export-fidelity evidence, and no issue was changed or created. The local PR body (`.dev/pr-body.md`, git-ignored) says `UNASSIGNED`, so the request-to-output evidence gate fails until the parent supplies a valid issue. The remaining open evidence: signed-in device timing, the shared file compared with the preview, a UI test of the pending composer.

## Review round 3: coherent receipt across replacement

This round used local editing and native tests only. No Claude Code, external model, source-bearing service, upload or publication was invoked. Code and tests are committed as `f58b9b426ea3f3dac8815e76267f3f7124c86007`; the previous two P2 fixes remain intact.

The remaining P1 race was reproduced before the fix. `testCoalescedSaveCannotExportDuringRequestReplacement` pauses reconciliation immediately before the previous coordinator is cancelled and immediately before the replacement coordinator snapshot. At each pause, a G2 Save coalesces into the outstanding read and returns while the R1 file is still visible. Both cases enabled export and offered R1 before the fix (four assertions failed in one test, `/tmp/kria-race-red.log`). DEBUG-only checkpoint closures control these suspensions; the assertions exercise the session's public export availability and route.

`DeviceRenderSessions.savedRenderReceipt(for:)` now reads the accepted server request identity, bound generation and eligible file synchronously in one main-actor turn. It rejects active reconciliation, queued rereads (including the task-scheduling gap), absent bindings, request mismatch, non-exportable phases, repair requirements and missing files. Replacement invalidates the previous binding and presentation before cancellation can suspend. A new binding is accepted only after the coordinator snapshot returns and the reconciliation ticket is still current. Existing publication and observer paths retain their matching-request checks. The editor no longer stitches independent base-generation and request getters or caches a linked identity; it consumes this receipt and checks its generation against the acknowledged Save and current document.

Verification on the task-owned simulator, final code identical to `f58b9b426`:

- `/tmp/kria-race-final.log`: **413 tests, zero failures, zero reported skips**. Includes NativeEditorSessionTests 270, ProjectActionsTests 35, ProjectRestorationTests 2, NativeEditorExportTests 10, and four device-render session/transient/execution/repair classes totalling 96. Both controlled interleavings, sequential failed-status reproduction, valid same-generation path and existing verification/repair/missing-file controls passed.
- Earlier in this round `/tmp/kria-race-green.log`: 317 tests, one failure at the already documented `NativeEditorExportTests.testCancelledNativeEncoderDoesNotRetry` line 58. No new cancellation failure is attributed to this patch. `/tmp/kria-race-sessions.log`: 96 tests passed. The final aggregate above passed that export test too; this does not establish that the baseline flake is fixed.
- `git diff --check` passed.
- `/tmp/kria-race-preship-final.log`: seven gates pass; request-evidence fails with `issue must name a KRI ticket or its Linear issue URL`. The local body remains UNASSIGNED. No tracker was invented or changed.

### Verified TLC dependency recovery and remaining model gap

The already-existing cache `/Users/emirerben/Documents/Codex/2026-10-30/task/nova-overlays/.dev/formal-ios-edit-flow/tla2tools.jar` hashes exactly to trusted pin `fc6878c603f0fdc18b9fe3e5bf51860cb90ab5b1154e3c371660c036d6bdd63e`. It was selected using supported `TLA2TOOLS`; installed `/opt/homebrew/opt/openjdk@21/bin/java` was selected with `JAVA`. No checksum was weakened, mismatched bytes executed, dependency repinned or other worktree modified.

`JAVA=/opt/homebrew/opt/openjdk@21/bin/java TLA2TOOLS=/Users/emirerben/Documents/Codex/2026-10-30/task/nova-overlays/.dev/formal-ios-edit-flow/tla2tools.jar python3 formal/ios_edit_flow/verify.py` completed with **exit 3: PASS (no residual findings) with DRIFT to review** (`/tmp/kria-race-formal.log`). The default safety, liveness, historical and mutation checks completed. This resolves the artifact-execution blocker, not model correspondence. Drift remains 28 functions versus 26 at baseline; hashes were not resynced. Relevant existing rules include NoStaleRequestReinstalled, KeptIsThisRequest, NoStalePublish and PreviewConverges. The current model does not explicitly represent the export receipt/coalesced-reader race, so no formal proof of this new fence is claimed. Model maintenance, pending-composer UI evidence, signed-in timing, exported-file fidelity and valid tracker ownership remain open.

## Review round 4: bounded formal correspondence (native unchanged)

Native revision remains `5621edd443a06d6323e69f706ba225884013e67a`; this round changes verification and documentation only, using local tools. No external model or source disclosure was used. The maintenance guidance and repository verify-phone-render skill were read. The prior default TLC run's exit-3 PASS is retained as evidence about the existing lifecycle abstraction, not the previously absent receipt or replacement-editor behavior.

`formal/ios_edit_flow/SavedRenderReceipt.tla` adds a bounded companion with an explicit mapping in `saved-receipt-correspondence.md`. It covers one key, two immutable requests/generations, an active reconciliation, both replacement suspension windows, coalesced/reread task ownership, an eligible presentation/file, and a suspended Save across two editor identities. The default supported verifier now invokes this companion; it can also run independently with `verify_saved_receipt.py` using the same checksum-verified toolchain.

Final companion result (`/tmp/kria-receipt-model-final.log`): **464 distinct states, three safety invariants hold**, four separate mutation runs caught. `LegacyCancel` violates ExportMatchesSaved in five states; `LegacySnapshot` violates it in seven; `AllowUnresolved` violates NoUnresolvedExport in three; `LateSaveWrite` violates ReplacementEditorUntouched in five. The correct model's NeverNewExport reachability control fails in eight states, showing that the replacement really can be exported. These are semantic controls of the mapped abstraction, not automatically generated Swift mutants. Logs and the safe machine-readable report are in `.dev/formal-ios-edit-flow/receipt-*.log` and `saved-receipt-report.json`.

The module checks file-generation correctness at the export event, no export during unresolved/coalesced reconciliation, and isolation of the replacement editor after a suspended read. Native failing-first tests remain the executable correspondence evidence for the actual two suspension windows. The original lifecycle invariants and PhoneRenderBefore history are unchanged.

No coverage hashes were resynced. The review is scoped to the task's interleavings, not every branch of saveOnce, refreshDeviceRender or the unrelated baseline drift. Original coverage remains 28 drifted functions versus 26 on unchanged main, with the same two additional functions. Published-generation aliasing, cache TTL/authorization, owner/account changes, process death, verification/repair transport and media byte/frame fidelity remain native-test/source arguments or open gaps; the companion is not a full-function or end-to-end proof.

Later main integration is also open. PR #2117 merged at `19cc8fdcaa549bdb831adfdb0582b8bea609f206` after the reviewed native revision. This formal pass neither integrates that change nor claims the earlier native results cover it. A controlled integration and affected native rerun are required before publication. UNASSIGNED tracker ownership, pending-composer UI evidence, physical-device timings and exported-file inspection remain explicit blockers/gaps.

Formal run results for this pass: the supported `python3 formal/ios_edit_flow/verify.py` command completed **exit 3, PASS (no residual findings) with DRIFT to review** (`/tmp/kria-receipt-formal-full.log`). All unchanged lifecycle model, historical and mutation checks completed. That run included the initial three-run companion; the final standalone companion was then rerun after splitting the legacy control into cancel and snapshot windows, producing the four-run result above. Only the bounded mutation selection changed; lifecycle models and their rules were unchanged. The final standalone command is the same companion entrypoint called by the default verifier. No native tests were rerun during this verification-only pass; the previous 413-test result remains anchored to unchanged native code. Python syntax checks, ruff lint/format checks and `git diff --check` passed. None of this clears the 28-function drift or the publication gaps listed above.

## Controlled main integration and final validation

The clean pre-integration revision `52fb06ccf278d21ee747b624d9eac74af5a8444c` is preserved at local reference `backup/iphone-latency-before-main-integration-52fb06ccf`. Origin/main was fetched once and pinned to `1e91cb694165e6a0a0793cff21de6c74a3107918`. That snapshot includes #2117 (`19cc8fdcaa549bdb831adfdb0582b8bea609f206`) and #2103. It was integrated into this feature branch with the normal local merge workflow as `817ce18385a7d6bbb1bcbf1116f9aaec9dfcc9b4`; the automatic NativeEditorSession merge had no conflict. Neither side was discarded, no shared/main checkout was changed, and no newer main was chased.

The only post-merge source edits are debug fixture support and one bounded XCUITest, committed at `4ffa7aa49ac76038ea0603992a75fb5a21864203`. `ChatUITestTransport` can delay only POST `/creation-threads` under the explicit `KRIA_CHAT_CREATE_DELAY` test environment setting. No production create behavior, account state or backend data was changed. `testDelayedNewChatAcceptsTypingBeforeItsServerIdentityExists` delays the local fixture response 20 seconds, asserts pending UI appears, types into the real composer while pending, asserts Send disabled, waits for the real thread and asserts the typed text remains. It does not send a turn or upload media. No broad Creation/Projects UI suite was run in this integration pass.

All results below ran against source identical to the code/UI commit above, on task-owned iOS Simulator `3F0D9C92-2BA8-4831-9AD6-F070EF630845`:

| Check | Result | Evidence |
| --- | --- | --- |
| Rebuild + full KriaTests native unit aggregate | **2584 tests, 27 skipped, zero failures**, 493.328 seconds test execution | `/tmp/kria-integration-unit.log` and its xcresult |
| Affected suites within that aggregate | DeviceRenderExecution/IntentRepair/Session/Transient, NativeEditorExport/Session, ProjectActions/Restoration all passed; includes controlled receipt races, sequential failed GET, draft recovery, late-save replacement and cancellation case | same unit log |
| Exactly one delayed-New-Chat UI regression | **1 test, zero failures**, 27.741 seconds; pending composer typable, submission gated, text retained after real identity | `/tmp/kria-integration-new-chat-ui.log`; `/tmp/kria-latency-dd/Logs/Test/Test-Kria-2026.11.05_23-13-52-+0000.xcresult` |
| Supported formal verifier, including receipt companion/mutations | **exit 3: PASS (no residual findings) with DRIFT to review** | `/tmp/kria-integration-formal.log` |
| Diff whitespace | passed | `git diff --check` |
| Preship | Seven gates pass; request-evidence fails because `issue must name a KRI ticket or its Linear issue URL` | `/tmp/kria-integration-preship.log` |

Synthetic checks repeated in the integrated aggregate: with a 500 ms create-reply delay the pending surface was present at 0 ms and the real thread at 520 ms. With four 300 ms delayed Save calls, each asserted once, Save returned in 907 ms. These injected-delay results are not live timings. The historical actual baseline comparison (529 ms / 1224 ms on `56e21de1a`) remains a prior baseline, not a fresh A/B measurement of pinned main `1e91cb694`.

The intermittent cancellation/playback test passed in this integration run. That does not repair or disprove its documented failures on earlier unchanged main and feature builds. No native failures were hidden or retried to obtain the final aggregate result. No claim is made about unselected package/UI targets merely because the app unit target passed.

Coverage metadata was not resynced. Recomputed against the pinned main snapshot, baseline drift is still 26 functions and integrated drift 28; the two additional functions remain saveOnce and refreshDeviceRender. All four receipt mutation runs and lifecycle checks completed. Bounded-model limitations in `saved-receipt-correspondence.md` remain; full function correspondence is not established by an exit-3 PASS.

The fixture-backed pending-composer UI gap and the specifically requested #2117 main-integration gap are now closed to the evidence level above. Remaining publication blockers/gaps: UNASSIGNED tracker ownership, signed-in physical-device timing, inspection of the shared/exported file against preview/canonical render, and complete native/model correspondence beyond the bounded companion. No Linear issue was invented or changed. No external model/source disclosure, push, PR, merge into main or deployment occurred.

## Current-main bounded validation (2026-11-06)

Integrated main is `3d35555837fccd8d06e8c1b7d627cecb7ce13f98`; tested native code is `e43ce5bcc54668022a50cd012de85f217a2eebcf`. Independent local source review found no material integration defect; this is separate from runtime evidence. Cunda's unpublished native candidate was not incorporated.

At 06:56 UTC, the following bounded native run passed: **63 tests, 0 failures, 0 skipped** (35 ProjectActions, 18 selected NativeEditorSession, 10 NativeEditorExport). It covers pending text success/failure/retry/navigation, both controlled request-replacement suspension windows and coalesced reads, stale generation, verification/attention states, changed/deleted files, concurrent save/download, late Save into a replacement editor, and cancellation. The known intermittent cancellation test passed once here; its baseline flake is not resolved.

Log: `/tmp/kria-speed-bounded.log`; result bundle: `/tmp/kria-latency-dd/Logs/Test/Test-Kria-2026.11.06_06-53-59-+0000.xcresult`. Command (from `src/apps/ios`):

```sh
xcodebuild -project Kria.xcodeproj -scheme Kria -skipPackagePluginValidation -disableAutomaticPackageResolution -destination id=3F0D9C92-2BA8-4831-9AD6-F070EF630845 -derivedDataPath /tmp/kria-latency-dd CODE_SIGNING_ALLOWED=NO test -test-timeouts-enabled YES -default-test-execution-time-allowance 120 -maximum-test-execution-time-allowance 180 -only-testing:KriaTests/ProjectActionsTests -only-testing:KriaTests/NativeEditorExportTests -only-testing:KriaTests/NativeEditorSessionTests/testSyntheticSaveSerializationUnderInjectedCallDelay -only-testing:KriaTests/NativeEditorSessionTests/testSaveStartsThePhoneRenderWhileTheCommittedVariantReadIsStillOut -only-testing:KriaTests/NativeEditorSessionTests/testExportHandsOverThePhonesOwnRenderOfTheSavedEditBeforeAndAfterPublish -only-testing:KriaTests/NativeEditorSessionTests/testOwnRenderIsNotOfferedOnceTheCreatorChangesTheEdit -only-testing:KriaTests/NativeEditorSessionTests/testOwnRenderIsNotOfferedWhenItsFileIsGone -only-testing:KriaTests/NativeEditorSessionTests/testOwnRenderIsNotOfferedAfterANewerRemoteGenerationIsAdopted -only-testing:KriaTests/NativeEditorSessionTests/testOwnRenderIsNotOfferedWhenTheRequestChanges -only-testing:KriaTests/NativeEditorSessionTests/testOwnRenderIsNotOfferedWhileASecondSaveIsInFlight -only-testing:KriaTests/NativeEditorSessionTests/testOwnRenderIsNotOfferedWhileTheServerStillVerifiesIt -only-testing:KriaTests/NativeEditorSessionTests/testOwnRenderIsOfferedWhileItIsStillUploading -only-testing:KriaTests/NativeEditorSessionTests/testOwnRenderIsNotOfferedOnceTheServerNeedsAttention -only-testing:KriaTests/NativeEditorSessionTests/testFileDeletedAfterASuccessfulSaveToPhotosIsNotOfferedAgain -only-testing:KriaTests/NativeEditorSessionTests/testDownloadAndSaveRunTogetherWithoutDuplicatingOrSwappingTheFile -only-testing:KriaTests/NativeEditorSessionTests/testCoalescedSaveCannotExportDuringRequestReplacement -only-testing:KriaTests/NativeEditorSessionTests/testFailedStatusReadAfterANewSaveDoesNotLinkThePreviousRenderToIt -only-testing:KriaTests/NativeEditorSessionTests/testStatusReadThatNamesTheSavedGenerationLinksTheNewRender -only-testing:KriaTests/NativeEditorSessionTests/testRenderNotStartedSaveCannotWriteRetryStateIntoAnEditorLoadedMeanwhile -only-testing:KriaTests/NativeEditorSessionTests/testCancelledSaveLeavesNoFailureStateBehind
```

Synthetic injected-delay results: pending New Chat 0 ms, real thread 517 ms with a 500 ms POST delay; Save 906 ms with four 300 ms calls, each once. These are not real-device timings. Historical baseline and prior full-suite/UI results above retain their original tested SHA.

Current-head full native/UI and full lifecycle formal attempts were interrupted by disk exhaustion/resource priority and remain incomplete. This bounded run does not replace those broader checks. Physical-device timing, exported-file fidelity, broad formal correspondence and the cancellation flake remain open under restored In Progress **KRI-640** (not KRI-672; earlier UNASSIGNED references describe historical state). Local cache rebuild left more than 12 GiB free. No private project, backend data or unrelated cache was changed.

## CI inventory correction (2026-11-06)

Hosted run 42177880099 at `477b91f63900af5743e8b6bc902cd7d9052713fb` stopped in selection: 160 contract tests, four failures. No downstream native/API/journey execution occurred. Local reproduction matched all four failures; unchanged main `31477c8e9b0eabf5f143d4b506b9543a7c17f44c` passed all 160. The delayed-New-Chat UI test was the only discovered test missing from `ui-test-groups.json`; inventory validation correctly fell back to full coverage.

Correction: add only `KriaUITests/CreationUITests/testDelayedNewChatAcceptsTypingBeforeItsServerIdentityExists` to the existing creation group. Selector code, assertions and conservative fallback are unchanged. On the actual corrected worktree (parent `477b91f63`, inventory SHA-256 `97fff33eaeb3b61f48e97cc98ffb9af3f0f78e3b3204650957fcd47a41dfdee2`):

- `python3 -m unittest discover -s scripts/ci -p 'test_*.py' -v`: **160 passed**, 57.060s; `/tmp/kria-speed-registration-ci.log`.
- `python3 -m unittest discover -s scripts/ios/tests -p 'test_ui_tests.py' -v`: **26 passed**, 11.604s; `/tmp/kria-speed-registration-ui.log`.
- Inventory matches; shared AppState input still selects full; `git diff --check` passed.

These checks ran before this evidence-only report addition; all test inputs are identical to the committed correction. Native production/test/build inputs are unchanged from `477b91f63`, whose 63-test pass remains attributed to that SHA. Final-head native re-execution, preship and hosted results are recorded separately in PR evidence; this report does not preclaim their outcome. Main integration is not needed for this missing registration. Broader validation gaps remain as above.

## Pinned-main integration ad2f4af1 (2026-11-06)

Combined native/test/build inputs were verified at `ecbb6a8ee9f71d9b5fe82f99648ab6ad7a2a790b`, merging pinned main `ad2f4af1cc5890fe7ca9737bb2ede5645496793c` without conflicts. Backup: `backup/iphone-latency-before-main-ad2f4af-f22d2733`. Independent source review found no material integration defect. Both delayed-create and explicitly gated place-hint fixtures, six UI registrations and main upload withdrawal/cancellation guards are preserved. NativeEditorSession, DeviceRenderSessions and media-engine files are unchanged from the previously reviewed speed head. Unpublished Cunda code is not included.

Executed at the combined head:
- CI selection/required contracts: **160 passed**, `/tmp/kria-speed-ad2-ci.log`.
- UI-selection contracts: **26 passed**, `/tmp/kria-speed-ad2-ui-selection.log`.
- Focused native: **133 passed, 0 failures/skips**, `/tmp/kria-speed-ad2-native.log`; includes ProjectActions, 18 save/receipt session cases, NativeEditorExport, BackgroundUploadRetry and FootagePlacement. Bundle: `/tmp/kria-latency-dd/Logs/Test/Test-Kria-2026.11.06_08-13-01-+0000.xcresult`.
- Exact delayed-New-Chat plus five UploadPlaceHint UI tests: **6 passed, 0 failures/skips**, 134.315s, `/tmp/kria-speed-ad2-ui.log`; bundle `/tmp/kria-latency-dd/Logs/Test/Test-Kria-2026.11.06_08-14-55-+0000.xcresult`.
- Preship and diff whitespace checks passed.

Commands are preserved in `/tmp/kria-speed-ad2-bounded.sh`: project regeneration, scoped xcodebuild test, then serial test-without-building UI execution. Synthetic pending chat 0 ms / real identity 522 ms for 500 ms injected delay; Save 905 ms for four 300 ms calls, each once. No real-device speed claim.

This report-only successor preserves all consumed native/UI/selection inputs byte-for-byte. A fresh final-head focused native run and final preship are reported separately in the PR body. The six UI results above remain attributed to ecbb6a8ee. Full native hosted evidence from f22 is prior-head only. Lifecycle formal is still incomplete after resource interruption; it was not started during this integration validation. Physical-device timing, export fidelity, broad model correspondence and the historical cancellation flake remain open under KRI-640.

## Background-focus integration preparation (native execution pending)

Pinned main `05d9720d8b3f838f8b91963e183eb7206c34e538` (protocol 42 / #2108) merged cleanly at `dc50c3625b0399a236cb4cc72ed432017d07906b`. Backup: `backup/iphone-latency-before-focus-6d924dbb`. The complete main protocol, capabilities, decoding, editor and shared preview/export implementation is inherited together. No production correction was made and no interaction defect is established. Cunda's unpublished candidate is excluded. The green hosted run 42186925653 belongs to `6d924dbb48a454babcfc5cce4c393a51e6c030dc`, not this integration.

Four targeted native regression methods are prepared in NativeEditorSessionTests (NOT compiled or executed due Cunda priority):
1. Focus-only Save commits only the background lane and reuses the coherent same-generation local receipt.
2. A newer focus edit survives both successful and 503 post-save reads, stays dirty and refuses the old export.
3. Replacement editor state remains untouched by both successful and failed suspended focus-save reads.
4. Cached capabilities lacking backgroundFocus reject the replacement render; explicit Retry reads fresh, disabled responses do not grant the missing capability, and a later supported response restores eligible reuse.

The fixture uses synthetic exports and a schema-2 recipe carrying the committed background focus; it asserts accepted request focus and proves routing/identity behavior, not output pixels. Existing speaker background round-trip/undo/opening and shared compositor focus/below-cover black-band tests are selected for later execution instead of copied. Generic late-save render-not-started, cancellation, source-window and all existing receipt controls remain selected. These assertions are expectations awaiting execution, not passing evidence.

Prepared commands: `/tmp/kria-speed-focus-next-checks.sh` (do not run until resource release). No native/UI/formal work or publication occurred during preparation. Lightweight CI/inventory results are reported separately; no gate or coverage hash was weakened/resynced. Physical-device timing, exported-file fidelity and formal-correspondence gaps remain open under KRI-640.

Preparation checks on the integrated worktree: 160 CI selection/required-contract tests passed (73.874s, `/tmp/kria-speed-focus-ci.log`); 26 UI-selection tests passed (29.796s, `/tmp/kria-speed-focus-inventory.log`); `git diff --check` passed. These Python checks do not compile or execute the four new Swift methods.

### Focus fixture review correction (native execution still pending)

Corrected two review gaps without production changes. Suspended successful reads now return the commit generation `next` plus distinguishable authoritative text. The retained-editor case must adopt that text on success (and not on 503), while preserving the newer focus and its dirty/export-blocked state. The replacement editor deliberately uses the same generation string and different text; unchanged draft/text assertions therefore cannot pass merely because generations differ.

The synthetic render request now carries a validated schema-2 speaker background with the committed fill asset and focus `(0.2, 0.5, 0.5)`, deriving backgroundFocus capability from the actual layer. Assertions inspect the accepted request identity, asset and focus against the committed lane/current document. Exported fixture bytes are still only a routing stand-in; actual image/video fidelity remains the separately selected media-engine tests and device inspection. These revised Swift assertions remain uncompiled/unexecuted while Cunda has resource priority.
