# Post-render edit loop on iOS: verdicts on suspected bugs

Worktree: `/private/tmp/nova-ios-edit-flow-formal` at `ff27b0a65`. Charts: `editor-save.mmd`, `editor-device.mmd`, `editor-chat-review.mmd`, `slides-save.mmd`. All four render with mermaid-cli 11.4.2.

Terms:
- **Generation token (G):** the editor's `document.revision.baseGeneration`. The server compares it with `variant_render_baseline()` (`render_generation_id`, else `render_finished_at`; see `src/apps/api/app/routes/generative_jobs.py:10312`).
- **Lane:** one editable section of the video (text, timeline, music and so on).

## Suspected bugs

### E1: switching jobs mid-save leaves the editor stuck in "saving" with a stale base_generation. CHANGED (the real failure is different)
- `switchToLatestJob` has no `!isSaving` guard (`src/apps/ios/Kria/Features/NativeEditorSession.swift:1994`). But `load(editorJobID:)` resets `saveState` and the document to the new job (`:2049`, `:2125`). The stale save's result is then thrown away by `saveIdentityMatches` (`:5603`, `:7431`). So the editor never gets stuck in "saving", and the next save uses the new job's generation.
- The real problems:
  - (a) *Reviewer correction:* the save does NOT necessarily land on the old job. The commit URL has no job id, and the server commits to `item.current_job_id` (`src/apps/api/app/routes/plan_items.py:7635`). If the re-plan already made its job current, the commit gets a 409 (`baseline_conflict`, or `variant_initial_render_in_progress` while that job's first render runs). Only a commit that arrives before the switch lands on the old job. See N9 for what happens next.
  - (b) A re-plan that arrives during a save is dropped. The editor reloads on `.onChange(of: project.activeJobID)` (`NativeEditorView.swift:207`), which calls `adoptLatestJob`, which returns early when `isSaving` (`:1985`). That change never fires again.
- **User story:** You tap Save, and while it spins, Kria finishes a re-plan. The editor keeps showing the old video. Nothing tells you a new version exists until a later chat sync or a reopen notices it.

### E2: every 409 becomes `.conflict`, but only `baseline_conflict` is fixed by a rebase, so it loops. CONFIRMED
- `catch APIError.conflict` (`NativeEditorSession.swift:5741`) matches every 409, because all `ConflictDetail`s compare equal (`src/apps/ios/Kria/Core/Services.swift:2256`, `:2308`).
- The server's 409 reasons:
  - `Cancelled videos cannot be edited.` (`src/apps/api/app/routes/plan_items.py:7648`)
  - `variant_initial_render_in_progress` (`:252`)
  - `deletion_target_stale` (`:7686`)
- `rebaseAfterConflict` (`NativeEditorSession.swift:5785`) reloads the variant and puts your lanes back. Saving again hits the same 409.
  - For a cancelled video, this loops forever.
  - For an initial render still running, it loops until that render finishes.
- **User story:** You edit a video whose render was cancelled and tap Save. You see "A newer edit exists" and tap "Reload latest and keep my edits", then Save again. You get the same banner every time.

### E3: the conflict rebase silently overwrites another device's edits in the same lane. CONFIRMED
- `rebaseAfterConflict` copies each locally changed lane wholesale from your document over the latest one (`NativeEditorSession.swift:5815-5817`). There is no per-item merge and no notice.
- **User story:** On your iPad you change caption 3. On your iPhone you change caption 7 and Save. You tap "Reload latest and keep my edits", then Save. The iPad's caption-3 change is gone, and nothing says so.

### E4: undo-stack math is wrong at the 100-step cap or during a save. CONFIRMED (at the cap)
- `saveOnce` records `submittedUndoCount = undoStack.count` (`:5593`). After the save it keeps `undoStack.dropFirst(submittedUndoCount)` as the undo history for edits made during the save (`:5606`). `appendUndo` trims the stack to 100 (`:1442-1446`).
  - When the stack is already at 100, every edit made during the save trims the oldest entry, so the count stays at 100. `dropFirst(100)` then returns nothing, and all undo steps for edits made during the save are lost.
  - An undo during the save shrinks the stack, which shifts the slice the same way. The toolbar Undo is disabled while saving (`NativeEditorMediaViews.swift:1589`), but the caption-removal "Undo" notice is not (`NativeEditorLanePanel.swift:401`).
- **User story:** After a long editing session (100+ steps) you tap Save and keep typing while it saves. When the save lands, Undo is greyed out, even though your new typing is still unsaved.

### E5: an undone staged chat edit comes back from the server's draft head. CONFIRMED (after a full reload)
- Within one session, `appliedChatDraftRevision` stops the same draft revision from being staged twice (`Services.swift:2716`). But it lives only in memory. Both `load` paths reset it to nil (`NativeEditorSession.swift:1638`, `:2143`), and the server's draft head still holds the chat edit.
- *Reviewer narrowing:* this only happens if nothing was saved in between. Any save bumps `render_generation_id`, which makes the draft stale, so it is not staged again (`Services.swift:2718`). There is also no server-side undo of the draft head when you tap Undo locally (`undo()` at `NativeEditorSession.swift:5566` only pops the local stack).
- **User story:** Kria makes a chat edit, and you tap Undo to remove it. You leave and reopen the editor, and a full reload runs (for example, after a newer render). The chat edit you removed is back as unsaved changes.

### E6: Review "Undo all" auto-approves the newest approval, which may be the wrong one. CONFIRMED (race)
- `undoAllReview` passes `turnID: ""` (`ChatWorkspaceView.swift:768`). `approveReviewDraft` then approves the newest `approval_requested` event after the start point, with no turn filter (`:778-781`).
- *Reviewer addition:* even with a real turn id, an `approval_requested` event with no `turn_id` in its payload matches any wait (`:780`), so scoped updates have the same exposure to an id-less approval.
- The server's undo-all mints its own render turn that waits for approval (`src/apps/api/app/routes/kria_runtime.py:541`). If another turn's approval lands in that window, the wrong one is approved, and the undo-all approval stays pending.
- **User story:** You send a chat request, then quickly open Review and tap Undo all. Kria approves your chat request's render instead of the undo. The sheet says "Updated", but your sections were never restored.

### E7: Review's follow step stalls for 600 s on a same-job render. CONFIRMED
- `follow(awaitNewJob: true)` finishes only when the snapshot is `ready` on a job different from the starting one (`src/apps/ios/Kria/Core/ReviewPlanModel.swift:265`). But scoped editor updates and section undos "re-render the SAME Job" (`src/apps/api/app/tasks/kria_runtime.py:5276`). The snapshot's `job_id` comes from the latest plan block (`src/apps/api/app/kria/plan_snapshot.py:144`), so it never changes.
- The unit tests assume the job changes ("job-1" to "job-2" in `src/apps/ios/Tests/KriaTests/ReviewPlanTests.swift:97-99`), so they miss this.
- **User story:** You flag captions in Review and tap Update video. The video updates, but the sheet keeps showing "Updating" for 10 minutes and then says "This is taking longer than usual."

### E8: a slide save overwrites edits made during the save. CHANGED
- `draft = saved` after the await (`src/apps/ios/Kria/Core/SlidePost.swift:899`) cannot overwrite mid-save edits, because `stageDraft` refuses any edit while `isBusy` (`:1074`) and the editor controls are disabled. Instead, edits made during the save are silently dropped.
- *Reviewer narrowing:* the sticker case is narrower than first written. While a save runs, `refresh` returns early (`:751`, `:759`), so a NEW picture is simply not found yet and the poll keeps waiting (120 x 1 s, `:1286`). It is placed after the save. The false "slide is full" notice happens only when the picture's asset is ALREADY ready in the local state, which happens when you paste a picture the pool already holds (the pool dedupes by file name, `:1263`), and the upload returns while a save or a "Kria is arranging" propose is in flight. Then `addSticker` → `stageDraft` drops it (`:1074`) and `:1270` shows the wrong message. A save that outlasts all 120 polls shows "taking longer than expected" instead.
- Separately, a 409 refetches only the version number and writes your draft over the server's newer one, up to 2 times, with no notice (`:887-893`, KRI-298 design).
- **User story:** You paste the same photo onto a second slide and tap Save before the paste finishes. The sticker never appears, and the banner says "A slide can have up to N pictures", which is false.

### E9: killing the app loses unsaved video edits, despite copy saying they are stored on the device. CONFIRMED
- The exit dialog says "Unsaved editor changes are stored only on this device until you save." (`NativeEditorView.swift:241`). But `NativeEditorSession` never persists the document: there are no UserDefaults or file writes for it, and no scene-phase save. Slides, by contrast, persist on every change (`SlidePost.swift:676`, `:1562`).
- **User story:** You make 10 caption edits, switch to Messages, and iOS evicts Kria. When you come back, the video is back to its last saved state.

### Extra: a second Save tap is dropped. CONFIRMED (low impact)
- The Save button is disabled while saving (`NativeEditorProjectHeader.swift:91`). But "Save and exit" during an in-flight save calls `save()`, the `!isSaving` guard drops it (`NativeEditorSession.swift:5586`), and `saveAndExit` returns because there are still unsaved changes (`NativeEditorView.swift:977`).
- **User story:** You tap back while a save is spinning and choose "Save and exit". The dialog closes, and you are still in the editor with nothing new saved.

## New stuck or degraded paths

- **N1: the preview never matches (stuck on that button, with an escape).** The poll ends only on `ready` or `failed` for its own generation (`NativeEditorSession.swift:7296-7342`). If another device's save moves the variant to G2, it polls for 5 minutes and then shows "Preview needs another check". Tapping "Check preview again" repeats the 5 minutes forever (`:7286`). *Reviewer correction:* `cancelled` and `superseded` are not persisted variant statuses. `"cancelled"` is only a worker return value (`tasks/generative_build.py:3836`), and superseded workers just drop their output. So the trigger is a G2 from another save. The escape: edit again and Save, which gets a 409 `baseline_conflict`, then "Reload latest" moves you to G2. Reopening the editor also works.
  - **User story:** You save on your phone right after saving on your iPad. Your phone keeps saying "Preview needs another check" no matter how often you tap it.
- **N2: a chat edit never shows, then is dropped (stuck, then degraded).** You and a chat edit change the same lane, so you get `.conflict` (`:1802`, `:1835`). "Reload latest and keep my edits" rebuilds from the rendered variant, not from the chat draft (`:5795`), so the chat edit is not shown. The next sync raises the same conflict again. If you Save, the chat draft's base_generation is stale, and the chat edit is silently discarded (`Services.swift:2718`).
  - **User story:** You ask Kria to restyle captions while you are editing a caption. You get a conflict banner, reload, and Kria's restyle never appears. After you Save, it is gone for good.
- **N3: a capabilities fetch failure looks like "can't render here" (degraded, recoverable).** Retry refetches capabilities (`retryDeviceRender` → `refreshDeviceRender(retry: true)`, `:7435`), so it recovers once you are back online. Nothing is reported to the server for this path. `refreshDeviceRender` turns any `creationCapabilities()` error into `.disabled` (`NativeEditorSession.swift:812`). The edit is then routed as "Rendering on iPhone is not available for this edit yet" (`DeviceRenderSessions.swift:146`, `:327`).
  - **User story:** On flaky Wi-Fi you save a phone-rendered edit. You are told iPhone rendering isn't available for this edit, even though it is.
- **N4: a brief offline blip marks the device render failed on the server (degraded).** In `perform`, the first `isCurrent` call throws when offline. With no output file yet, that becomes `needsAttention`, and a failure is reported to the server (`DeviceRenderCoordinator.swift:348`, `:388-396`).
  - **User story:** You save in an elevator, and the phone render immediately says "needs attention", which needs a server retry instead of just waiting for the connection.
- **N5: no background task around the export (degraded), and the 24-hour reaper (stuck).** The export has no `beginBackgroundTask`. If you leave the app, the export fails and reports `needsAttention`. If the app is killed, the receipt stays on disk and is recovered only when the project is reopened (`DeviceRenderCoordinator.swift:253`). Otherwise the server shows awaiting_device for up to 24 hours until `device_render_reaper.py:96` fails it.
- **N6: a second reconcile is skipped (degraded).** `reconcile` returns immediately when a check for the same key is already running (`DeviceRenderSessions.swift:116`). So the post-save refresh can leave `pendingDeviceRenderIdentity` set to the previous request (`NativeEditorSession.swift:5734`), and the identity fence in `applyPreviewVariant` (`:7303`) may never match. This is plausible but I haven't reproduced it. *Reviewer:* more likely than it looks. The chat workspace's own poll reconciles the same `model.deviceRenders` key while a render is unsettled (`ChatWorkspaceView.swift:2234`, `:2272`), so a ticket is often already held. A device publish changes the generation (`published_generation = published_attempt`, `src/apps/api/app/services/device_render.py:125`), so the identity fence really is consulted. The result looks like N1.
- **N7: sending a chat message is blocked by an editor conflict (works as designed, but a dead end).** When editor-state turns are off or the state is too big, the chat send saves first. If that save conflicts or fails, the message is held: "Save or resolve your editor changes before sending it." (`ChatWorkspaceView.swift:1980-1985`). Combined with E2 (cancelled video), you can't send chat messages from that project at all.
- **N8: a Review "Undo all" or "Update" approval match that times out after about 60 s (150 × 0.4 s) shows a notice, but the server turn may still be waiting for approval** (`ChatWorkspaceView.swift:797`). This is plausible and I haven't verified the server side.

- **N9: "Keep editing" after a re-plan means every save conflicts (stuck: you can keep your edits or save them, not both).** The server commits to the plan item's current job (`plan_items.py:7635`), but the editor keeps the old job's `base_generation`. `rebaseAfterConflict` reloads the variant of the OLD `jobID` (`NativeEditorSession.swift:5795`), so it hands back the same stale G, and the next Save gets 409 again. The only way out is "Switch", which discards your edits (`NativeEditorView.swift:217`). The same loop follows E1 when the save hit the new job.
  - **User story:** Kria finishes a new version while you are editing, and you tap "Keep editing" to keep your caption changes. Every Save says "A newer edit exists", and "Reload latest and keep my edits" changes nothing. To save anything, you have to switch and lose your edits.

## Reviewer notes (skeptical pass)

- I checked every `%%` ref. Fixed: E3 lane copy is at `:5818`. Slides `hasUnsavedChanges` is at `:737` and persist is the `draft` didSet at `:677`. Review approval refs are now `:768`/`:775`/`:781`. The device retry is at `:7435`.
- Charts changed:
  - **editor-save:** 409 covers both "cancelled" and "first render running". 503 is added to save failures. Added a chat sync conflict edge (`:1835`), the song-already-removed edge (`:5744`), an escape from the preview loop, and the N9 old-job loop state.
  - **editor-device:** Retry now forks into a server retry or a local re-check. A sync failure surfaces to the editor.
  - **editor-chat-review:** Undo all now goes through Submitting, and the wrong approval is a race inside it.
  - **slides-save:** the sticker edge is narrowed.
- Refuted claims (inside the verdicts above): E1(a) "save lands on old job" (usually 409 instead). N1 "cancelled/superseded status". E8 "any paste mid-save is lost" (only an already-pooled picture).
- Everything else held up after tracing guards, retries and UI disabling. E2, E3, E4, E6, E7, E9, E-save2, N2, N4 and N5 are still CONFIRMED/NEW.

## Generation-token summary (for TLA+)

- **Save:** the client sends G (`document.revision.baseGeneration`).
  - The server refuses with 409 `baseline_conflict` when G is not the baseline (`plan_items.py:7680`). Otherwise it persists, bumps to G1, and enqueues one render.
  - Workers drop renders for a superseded generation (`tasks/generative_build.py:424`).
- **Acknowledge:** the client sets G := G1 (`NativeEditorSession.swift:6571`) and polls for `ready` with generation G1.
- **Device renders:** these may publish a different generation. It is accepted only when `publishedGeneration` matches and the request identity is unchanged (`:7303-7310`).
- **Rebase:** sets G := the variant's current `render_generation_id` (`:5797`).
- **Chat drafts:** staged only while the draft's base_generation equals the variant's `render_generation_id` (`Services.swift:2718`).
