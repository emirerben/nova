# Creation flow: verdicts on suspected bugs

Checked against worktree `codex/ios-edit-flow-formal` at `ff27b0a65`. All paths are relative to the repo root.

## C1: v2 approve fails with a non-speech 409, the card stays, every tap fails. CONFIRMED (for some 409 codes)
- iOS: `src/apps/ios/Kria/Features/ChatWorkspaceView.swift:2598` (only speech-cleanup codes get special handling) and `:2621` (every other error shows "Kria couldn’t record that decision.").
- Server: `src/apps/api/app/kria/runtime.py:2465` (`approval_stale`) and `:2535` (`draft_stale`) raise 409 and leave the approval **pending**.
- Approval refresh: `ChatWorkspaceView.swift:2515` re-fetches the approval only when its id changed or it expired. The same approval keeps its old fingerprint, so nothing changes on the client.
- Not affected: `thread_revision_stale` (`runtime.py:2456`). The refresh moves the revision forward, so the next tap works. A stale target (`runtime.py:2426`) cancels the approval and posts a chat message.
- Reviewer check: `approval_stale` is effectively unreachable. `approval_fingerprint` (`runtime.py:155`) hashes only immutable pins of the approval row, so a re-fetched copy of the same approval always matches. The real loop is `draft_stale` (`runtime.py:2535`). It fires when the draft head moved after the approval was minted. `write_draft` (`src/apps/api/app/kria/drafts.py:503`, reached through `PUT creation-threads/{id}/draft` at `src/apps/api/app/routes/kria_runtime.py:506`, iOS `Services.swift:1441`) inserts a new head but does **not** cancel pending approvals. `undo_draft` (`drafts.py:578`) does cancel them, so the two writers disagree.
- Sending a chat message does not escape either. `submit_turn` queues it behind the `awaiting_approval` turn (`runtime.py:991-994`), and a second message gets 409 "A follow-up is already waiting. Approve or dismiss the pending render first." (`runtime.py:978-986`). Only "No thanks" or expiry clears it.
- Story: You tap "Create this video". Meanwhile a manual draft save landed, for example from the editor on another device. You see "Kria couldn’t record that decision." You tap again and get the same message, every time, until the approval expires or you tap "No thanks". Nothing on screen tells you the plan is out of date.

## C2: "review the plan again" (manifest hash), where Refresh is rejected forever. REFUTED
- `src/apps/api/app/routes/creator_agent.py:4972` raises 409 "Footage or capabilities changed; review the plan again".
- iOS `src/apps/ios/Kria/Features/CreationConfirmationStage.swift:41` shows that sentence and offers "Refresh the direction". The button sends the canned message (`ChatWorkspaceView.swift:1859`).
- The re-plan writes a fresh hash: `creator_agent.py:3848` sets `locked.manifest_hash = manifest.manifest_hash`, and planning reads the manifest with the same arguments that confirm uses (`:2590` and `:4937`). The refresh therefore clears the conflict.
- One caveat remains. If footage is still changing, for example a clip still being processed, the next Create can be rejected again. That is one more round, not a loop.
- Story: You tap Create after changing a clip. You see "Footage or capabilities changed; review the plan again" with a Refresh button. Kria re-plans, and the next Create works.

## C3: unknown server format leaves "Opening your project…" up forever. REFUTED (the hang); CHANGED into a degraded path
- `SlidePostRouting.isFormatKnown` (`src/apps/ios/Kria/Features/SlidePostRouting.swift:30`) counts the format as known as soon as the thread has loaded, or the list row has any `editFormat`. It does not depend on whether `CreationFormat` can name the format. `resolveFormat` (`ChatWorkspaceView.swift:1338`) gives up after 5 tries and shows "Couldn’t open this project. Try again". So there is no endless spinner.
- Degraded instead: a planned-video thread (`src/apps/api/app/routes/creation_threads.py:4304`) has only `edit_format` set, for example `day_vlog`, `single_hero` or `narrated_ready`. `CreationFormat(editFormat:)` (`ChatWorkspaceView.swift:2994`) has no case for these, so `selectedFormat` stays nil. That makes `canAttachMedia` false (`:538`), so the + button does nothing, and the clip limit falls back to 50.
- Story: You open a video that came from your content plan. It plays fine. Later you try to add more clips to ask for a new version, and the attach button does nothing, with no explanation.

## C4: a stuck phone render shows "rendering" for about 24h with no client timeout. CONFIRMED
- The server waits `device_render_stale_after_s = 86400` (`src/apps/api/app/config.py:434`). The reaper then marks the render `timed_out` (`src/apps/api/app/tasks/device_render_reaper.py:55`). The client has no watchdog of its own.
- Worst case: `refreshDeviceRender` (`ChatWorkspaceView.swift:2268`) returns early when `capabilities?.phoneRendering` is nil. The panel then shows its default "Preparing on your iPhone" (`DeviceRenderPanel.swift:19`), and "Stop rendering" calls `sessions.cancel`, which finds no coordinator (`src/apps/ios/Kria/Core/DeviceRenderSessions.swift:265`). The button does nothing.
- Reviewer nuance: `loadCapabilitiesWithRetry` (`ChatWorkspaceView.swift:1349`) tries five times when the view opens and then stops. The capabilities RecoveryCard only appears on the format stage (`:1635`), so nothing on the rendering screen retries. Leaving the project and opening it again re-runs the load, which is the only way out before the 24h reaper.
- Story: You approve a video on an iPhone where the capability check didn’t load. The card says "Preparing on your iPhone". Stop does nothing. The project stays "Rendering" until the server gives up a day later.

## C5: generic failed card without `execution_id`, where Refresh only reloads. CONFIRMED (degraded, with a way out)
- `ChatWorkspaceView.swift:1805-1813`: without `failedRenderExecutionID` the button reads "Refresh project" and calls `refreshNow()`, which just reloads the project.
- The comment at `CreationConfirmationStage.swift:141-146` claims "Refresh project" asks Kria for a fresh draft. The code does not do that.
- The body text does say "or tell me what you want to change", and the chat box still works, so you are not fully stuck.
- Story: Your video fails. You tap "Refresh project", the screen blinks, and the same card comes back. Only typing a message gets you moving again.

## C6: in a Slider thread, `send()` silently drops the message. REFUTED (not reachable in practice)
- The guard at `ChatWorkspaceView.swift:1951` does return early for `.slides`. But a Slider project always shows `SlidePostWorkspaceView` (`SlidePostRouting.swift:69`), whose AI sheet has its own `send` (`src/apps/ios/Kria/Features/SlidePostAISheet.swift`). The `editorConversation` handed to the Slider workspace is stored but never drawn (`src/apps/ios/Kria/Features/SlidePostWorkspaceView.swift:80`).
- The only way to reach the guard is an edge case: you are in the format picker (`isChoosingFormat`) while another device switches the project to Slider. The typed text stays in the box and nothing is sent.

## Raw /confirm 409 strings shown verbatim. CONFIRMED (degraded)
- The confirm controller (`creator_agent.py:4745-5485`) has 17 places that return 409. Any multi-word detail that `CreationConfirmationStage.swift:41` does not recognise is shown as-is, with a "Refresh the direction" button. Examples: "Creator retry changed", "Creator execution is not resumable", "Speech cleanup recovery changed". The whole file has about 33 distinct 409 detail strings. Several are engineering wording, for example "Creator render target is incomplete".

## Stop during a phone render when the server rejects the stop. CONFIRMED (plausible)
- `DeviceRenderCoordinator.cancelByUser` (`src/apps/ios/Packages/KriaMediaEngine/Sources/KriaMediaEngine/DeviceRenderCoordinator.swift:280`) reports the stop with fire-and-forget (`:212`), and errors are dropped.
- If the report fails (409 or offline), the iPhone shows "Rendering stopped" while the job stays `awaiting_device`. The project list keeps saying "Rendering" until the 24h reaper runs. `recover()` refuses cancelled receipts (`:253`), so only "Try again" moves things forward.

## NEW findings

### N1: an upload left in limbo after the app is killed locks Send (stuck, LOW confidence after review)
- `src/apps/ios/Kria/Core/BackgroundUploads.swift:1291-1313`. At launch, a record with no live upload task and `retryCount >= 1` gets `.keepForManualRetry`, and the code just does `continue`. It never sets `uploadFailed`. `inProgressRecords` (`:969`) counts every record that is not failed, so `pendingUploadCount > 0` holds.
- Effects: the composer stays blocked (`ChatWorkspaceView.swift` `blocksSubmission`), and `decide()` keeps saying "Wait for your footage to finish uploading." (`:2555`). No failure row appears, so there is no Retry button.
- Reviewer check: on a force-quit, iOS normally re-delivers `didCompleteWithError` (cancelled) for the old background task when the session is recreated. `completed()` (`BackgroundUploads.swift:1369-1391`) then finds `retryCount >= 1` and calls `reportFailure`, which sets `uploadFailed` and shows Retry. So the limbo exists only if that callback never arrives, for example a task that was never created because `retry()` failed after reserving. It is real in code, but needs an unusual OS or network sequence.
- Story: You force-quit Kria during an upload that had already retried once. If iOS doesn't report the cancelled upload when you come back, Send stays grey, with no error and no Retry.

### N2: the iPhone decides "use cloud" but nothing reroutes (stuck)
- `KriaMediaEngine/Capabilities.swift:20-42` returns `route: .cloud` when a capability is missing, the device is hot, storage is low, or the renderer version is unsupported. `DeviceRenderSessions.swift:145` only shows needsAttention with the raw English reason, for example "Renderer does not support required capabilities". It does not report a failure to the server and does not hand the job to the cloud.
- Reviewer check: the coordinator's own `start()` *does* report a cloud route to the server (`DeviceRenderCoordinator.swift:236-247`). But `DeviceRenderSessions.reconcile` checks the route first (`:145-157`) and returns before it ever calls `start()`, so for an awaiting job that report never goes out. Because the server's `reasonCode` is nil, the friendly copy (`DeviceRenderPanel.swift:139-161`) falls back to the raw English reason. It also skips the hide-Try-again rule for `unsupported_recipe`.
- "Try again" calls `/device-render/retry` (`DeviceRenderSessions.swift:199`) and then reconciles into the same check. For the heat case it clears once the phone cools down. For missing capabilities it repeats, and the server waits 24h.

### N3: a Slider post with no plan item id spins forever (degraded, low confidence)
- `SlidePostWorkspaceView.swift:88` and `:735`. If `itemID` is nil and the single `refreshOwner` call fails, the screen shows "Setting up your post…" and never retries. Back still works.

### N4: partial cloud success counts as Ready (degraded)
- `variants_ready_partial` contains `_ready`, so iOS maps it to `.ready` (`src/apps/ios/Kria/Core/Services.swift:697`). Failed versions get a per-version Retry only on runtime v1 (`ChatWorkspaceView.swift:1760`). On v2 the failed versions stay as version buttons that are greyed out (`.disabled(variant?.isPlayable != true)`, `:1763`), with no reason and no retry.

### Other notes for the TLA+ model
- `WorkspaceStage.resolve` (`ChatWorkspaceView.swift:2645`) checks in this order: retry approval plus a new plan goes to direction; preparation active goes to rendering; preparation failed goes to failed. After that it goes by status: rendering wins over a new plan, while ready or failed plus a pending plan goes to direction. For a draft: pending plan, then choosing a format, then a format already picked (footage), otherwise the format picker.
- `ProjectStatus` (`Services.swift:671`) has one edge case: an active job id with no job row maps to `.rendering`.
- The "Thinking" indicator ends only on settling events (`ChatWorkspaceView.swift:2761`). It has no client timeout.

### N5: Slider export can render on the server and time out (missing from the first draft)
- `SlidePostExport.export` (`src/apps/ios/Kria/Features/SlidePostExport.swift:128-171`) renders on the iPhone only when `SlidePostOnDeviceRender.supports` is true. Otherwise it saves, calls `generateSlidePost` (`src/apps/ios/Kria/Core/SlidePost.swift:1039-1054`, "Creating your post…"), and polls every 1.5 s up to 200 times. After that it fails with "Your post is still rendering. Try again in a moment." (`:158`). Other endings are "Rendering failed. Try again." (`:165`) and Photos access refused (`:136`). Tapping Export again re-polls, and dispatches a new render only when none is running and fewer than 2 were started.
- Story: You tap Save to Photos on a post the phone can't build. You wait about 5 minutes on "Creating your post…", then see "Your post is still rendering". Tapping again usually picks up the finished render.
