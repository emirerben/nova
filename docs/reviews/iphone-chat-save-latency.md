# iPhone latency: New chat, Save, export after Save

Branch `codex/iphone-chat-save-latency` (worktree `/Users/emirerben/Documents/Codex/2026-11-05/task/nova-iphone-latency`), based on `origin/main` `56e21de1ab785c99f30cd09e5c3e2d7466240af2` (includes #2105 Steer now and #2118). **Tested code commit: `4cc324f6c0b0d3bd8561ad87ae07e26e1a23d7d6`** (the commit after it only adds this report). Local only: not pushed, no PR, nothing deployed.

## Evidence level

**Offline and Simulator unit evidence only. No signed-in device, no live network, no live speedup claim.** All timings below labelled SYNTHETIC use an injected delay and prove call structure, not real latency. The existing explanation that rendering dominates "Save feels slow" remains **unmeasured**. Historical Linear numbers (KRI-1185 ~52 s, KRI-1184 369 s) were not reproduced and are not baselines.

## What was awaited (read from code)

- **New chat:** one `POST creation-threads` before anything appeared (KRI-640 / #1607 had already removed the capabilities and read-back trips). The button was also disabled while the project list loaded (`isLoading`), so a tap during that read did nothing.
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

`bash scripts/preship-check.sh --pr-body .dev/pr-body.md`: all 8 gates green (ruff skipped, no API changes; drift, release metadata, reliability gates, journey mapping with 11 journeys for CI to run, request-to-output evidence accepted as `local_fix` for KRI-672). Without a PR body the evidence gate fails by design; the local body is `.dev/pr-body.md` (git-ignored) and its `recovery` row is unit-level only, `request_fulfillment` and `export_fidelity` stay pending for a signed-in device run. KRI-672 is the related ticket from the handoff, not confirmed as this work's issue.
