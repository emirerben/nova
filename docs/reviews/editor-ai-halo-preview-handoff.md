# Preview handoff: source trace and proposed ownership boundary

Latest landed-main candidate and validation limits: [KRI-1230 main integration](editor-ai-halo-main.md). Older integration/hold statements below record their original candidate.

Tracking: [KRI-1230 — Show AI editing activity in the iPhone editor](https://linear.app/kria/issue/KRI-1230/show-ai-editing-activity-in-the-iphone-editor). Native checks remain pending.

This records the standalone candidate and investigation. The authorized local integration branch is described in [the integration record](editor-ai-halo-integration.md), which supersedes the proposed implementation/ownership statements below.

This is a proposal for coordination with the Cunda session owner. It is not an implemented retention mechanism. The inspected motion base is `05d9720d8b3f838f8b91963e183eb7206c34e538`; the Cunda comparison is `a7ed058ba570d62add9fb2ee9df2cc6aa91ade5a`. No session/player code was edited by this investigation.

## Existing path

| Boundary | Current implementation | Consequence |
| --- | --- | --- |
| Turn settlement | `CreationWorkspaceView.settleThinking` clears the correlated turn. `refreshDelta` then calls `editorSession.synchronizePromptRevision` for fresh events when thinking has ended. | Conversation completion is distinct from preview completion. |
| Draft application | `runPromptRevisionSync` reads the authoritative draft/variant, rejects stale refresh sequence/baseline responses, merges submitted/local editor state or stages the new chat document. `document.didSet` calls `scheduleSourcePreviewUpdate`. Some paths also explicitly schedule a rebuild or call `prepareSourcePreview`. | Preview activity must follow the session's rebuild, not extend the conversation with a timer. |
| Cached rebuild | `scheduleSourcePreviewUpdate` cancels the older task and advances `sourcePreviewSequence`. `rebuildSourcePreview` may await `LivePreviewComposition` while `sourcePreviewState` remains `.ready`. Settlement uses `sourcePreviewSettledSequence`. | Checking only `.preparing` misses real cached work after the turn ends. |
| Uncached rebuild | Missing clip, audio or media inputs set `.preparing` while resolving. `prepareSourcePreview` also enters `.preparing`. | This path can hide the previously displayed picture. |
| Display admission | `canDisplayCurrentPlayer` admits `.preparing` only for the current finished render, with no dirty or chat-staged changes. `NativeVideoPreview.body` checks that admission before its `.preparing` branch. | A retained source player is rejected; the video is replaced by the preparation surface, including "Applying your edit" for chat-staged changes. |
| Replacement | The compiler may update the existing live composition in place. Otherwise a new `LivePreviewComposition` is created and guarded against stale sequence/document/pending-text state. Warming is currently limited to a playing finished-render handoff. `installPlayer(item:)` pauses and detaches the old item before assigning the replacement. | Merely keeping the view visible during `.preparing` does not solve the entire handoff. The replacement can still detach the old item before the new item is ready. |
| Replacement readiness | The source path sets `.ready` after installing the composition. `installPlayer` separately observes the item's asynchronous `.readyToPlay` / `.failed` state. | Composition construction, source-state settlement and AVFoundation readiness are distinct boundaries. |
| New job | `adoptLatestJob` retains the old job while readiness returns `.rendering`, exposing `.newVersionRendering`. Once switching, `discardPlaybackForNewJob` cancels old work, advances sequence, clears the player/source/input caches and recovery identity. | A presentation for the retained old job can be terminal while the new job is still active. Retention must never confer mutation authority on that old job. |

Relevant baseline source locations: `ChatWorkspaceView.swift` (`refreshDelta`, `settleThinking`); `NativeEditorSession.swift` (`canDisplayCurrentPlayer` around 794, `applyStagedChatDocument` around 1981, `runPromptRevisionSync` around 2013, `adoptLatestJob` around 2430, `discardPlaybackForNewJob` around 2530, `prepareSourcePreview` around 3013, `refreshRebasedSourcePreview` around 3185, `scheduleSourcePreviewUpdate` around 3527, `rebuildSourcePreview` around 3556, `installPlayer(item:)` around 8733); `NativeEditorMediaViews.swift` (`NativeVideoPreview.body` around 790). Line numbers refer to the inspected baseline and will move after integration.

## Guards to preserve

Historical [KRI-535](https://linear.app/kria/issue/KRI-535/editor-first-open-after-a-chat-turn-flashes-the-old-render-a-changed) fixed old output flashing on first open after a chat edit. An initial playback URL is not proof that the user has seen a validated preview in this session. The existing first-open seed, dirty-document and staged-chat guards must remain.

Cunda's `EditorOperationIdentity` and editor load epoch / `editorLifetimeID` are the authority for asynchronous work. Do not create a second competing lifetime, bypass a generation guard, or allow a stale callback to turn a retained presentation into the new current preview.

## Least invasive proposed change

Separate presentation permission from editing/export permission. Keep the existing `canDisplayCurrentPlayer`, mutation ownership and export readiness meanings; do not broadly return `true` for retained players.

1. **Read-only activity projection.** The session owner exposes a computed pending-preview signal based on the existing deferred/rebuild/settled sequence state and load/failure state. It must remain true for a cached composition rebuild and turn false on settlement, failure, cancellation or lifetime replacement. Existing published document/source/load changes should drive observation; add no polling, synthetic delay or progress counter. The halo consumes that projection alongside `.preparing`.
2. **Validated presentation lease.** Keep one presentation record for a preview that was actually admitted and ready in the active editor session. Bind it to the session lifetime, project, job, variant and base generation, plus the displayed aspect ratio and player/item. A seeded URL alone cannot create this record. The record is display-only: it is not the current document, a source pool, an export route or permission to mutate an old target.
3. **Handoff display mode.** While the same owned session prepares a replacement, let the view present that record with explicit copy such as "Updating preview — showing the previous version." Keep it in the existing player-surface position. Suppress current-draft canvas objects, selection handles and hit testing over the previous picture. Save/export and transport readiness must continue to use their existing authority; never mark the previous picture current just to make a control enabled.
4. **Ready replacement commit.** Build and validate the candidate under Cunda's lifetime/target/sequence guards before retiring the displayed item. Reuse the existing readiness/handoff machinery where safe. The owner must cover the paused-source path as well as the currently warmed playing-finished-render path. Do not detach the previous player in `installPlayer` before the owned candidate is ready to take over. On a failure, show an honest previous-version/error state with existing recovery actions; do not animate success or silently change the document.
5. **Clear boundaries.** Drop the presentation lease on project replacement, explicit editor reset/dismissal, lifetime invalidation and any incompatible target change. Same-project generation/job transition must be an explicit owner-approved previous-version presentation, never identity mixing. First open without an admitted preview retains today's loading/failure behavior. Backgrounding pauses the decorative halo; returning reconciles real state and does not promote a cached lease to current.

The session owner should implement steps 1–5 or approve a narrower shared contract before the motion worker edits these paths. The motion change can then consume the pending signal and display mode with a small view/projection edit. This avoids concurrent edits to Cunda's ownership layer.

## Required checks when the native lane is available

- Reproduce a same-target chat edit after a preview has visibly loaded: one fully cached rebuild and one delayed new clip/audio/media resolution. Assert the old picture stays present, is labeled previous, and has no stale canvas interaction until the new owned item is ready.
- Delay candidate item readiness independently from composition creation. Confirm the old item is not detached early; cover paused and playing preview, scrub/seek during handoff, and playback-clock continuity.
- Failure, cancellation and a superseding prompt during both resolution and candidate warmup must not commit the wrong candidate. Confirm manual edits and undo history survive.
- Change project, job, variant and generation while a replacement is suspended. Old callbacks cannot restore a previous project or claim current output. Preserve the explicit unsaved-edit decision for newer jobs.
- Negative first-open control: give an untrusted/older seed after an AI edit. It must stay hidden; no previous-preview label or lease is created merely from the seed.
- Verify render publication/export remains blocked or ready according to the existing authoritative state, independently from which previous picture is visible.
- Capture and inspect normal and reduced-motion/transparency video-editor fixtures. The current delayed/no-op prompt tests alone do not establish any changed-document handoff.

All native checks above are pending. No native build, Swift test, simulator or physical-device command ran during this investigation.
