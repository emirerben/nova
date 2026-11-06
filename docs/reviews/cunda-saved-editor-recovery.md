# Saved editor recovery after render failure

Acceptance before implementation: a cold-open runtime-v2 failed video project with a complete current saved editor snapshot offers Open saved edit and the Editor switch. Opening uses the existing read-only editor loader and exact persisted lanes. The render remains failed; no planning, retry, dispatch or save occurs on open. A no-op chat reply and reopening do not remove this access or erase same-job local changes. Failed first renders without a saved snapshot, reference-only drafts, mismatched item/job/variant/generation, unknown format and slides do not expose video recovery. Real missing media and insufficient source windows still fail honestly.

Production read-only observation on 2026-11-06: the incident job's authored video slot requested 1.933333s from 1.92s source, in_s 0, no source_end_s, playback_rate null, unlocked, not removed. Job and variant remained failed with editor_render_recovery from November 5 20:04 UTC, before the source-window patch deployed. The variant pinned a v2 saved draft reference, while the original phone export remained recorded published. The November 6 white-text turn was a no-op and dispatched nothing. Approved admin diagnostics exposed only the pinned draft ID, not its body or base generation, and stripped the device recipe; current snapshot validity and exact recipe end/hold remain unverified. No production state was changed.

This change must apply through shared native contracts, with no project, account or title exceptions. Opening alone does not establish current-project recovery: legacy source-window preview/Save compatibility and physical iPhone Save/reopen/Photos export require independent evidence.

## Implemented contract

The full native creation projection now retains `editor_state_version`, `editor_draft` and `editor_draft_available`. Recovery requires a full snapshot matching the active item, job and selected variant, plus matching snapshot payload/base generation. A published-source generation alias needs the server's recomputed availability attestation; older servers retain the direct-generation path. Reference-only snapshots, malformed bodies, missing identities and unknown/slides formats grant no recovery. The chat offers Open saved edit and its Editor switch without relabeling the failed render.

Opening uses the existing editor loader unchanged for lane hydration. It arms the existing explicit Retry render flow only for a matching persisted failed saved edit. Ordinary unchanged Save still does nothing. Explicit retry resends the timeline through normal commit validation and generation fences; opening and Refresh dispatch nothing. Concurrent local edits survive and a second tap during the commit does not duplicate it.

The shared legacy numeric helper admits only a positive tail whose nearest 30fps ties-to-even duration equals authored duration within one microsecond, with at most half a frame plus one microsecond of padding. Strict fit/reanchor runs first. Native compilation uses the resolved original's duration for this compatibility exception; missing original duration grants no exception. Exact endpoints, explicit native spans, real freezes and non-grid overruns retain their existing behavior. A matching legacy tail opts its whole timeline into the frame clock, avoiding the old millisecond projection and derived 0.993x slowdown. The stored lanes are not rewritten.

## Executed evidence

Base: `3d35555837fccd8d06e8c1b7d627cecb7ce13f98`.

- Initial simulator attempts stopped at package cloning and object emission because disk was full. These were infrastructure failures, not a red behavioral result.
- After capacity returned, original production source files from the base were temporarily restored in this isolated worktree. The new tests using only base APIs executed: 9 tests, 15 failed assertions including one thrown `invalidTimeline`. Observed failures included lost saved-draft fields, absent unchanged retry, 0.993103448275862 playback rate, no padding, 6.933 versus 6.933333333 timeline end, and mixed-timeline refusal. Implementation and test bytes were restored automatically afterward.
- Final candidate: **232 simulator tests passed**, 0 failures, on iPhone 17 Pro / iOS 26.5. Selection: SavedEditorRecoveryTests (9), SavedDraftPreviewRangeTests (5), SlidePostRoutingTests (12), NativeEditorInteractionTests (33), NativeEditorRenderCompilerTests, NativeEditorSourcesTests (25), ChatDraftStagingTests (75). This includes actual generated 1.92s video plus image timelines, decoded frame attachments at the padded tail, no-op reopening, explicit retry commit payload, source identity guards and concurrent edit retention.
- A standalone execution of the production numeric helper passed 5,042 assertions across rates 0.25, 0.5, 1, 1.3, 2, 3.5 and 4, including exact incident geometry and non-grid/long-gap controls. This supports the numeric contract, not device export.
- One extra exploratory original-duration fixture failed with APIError.unsupported before its intended check. It was removed; no behavior claim or production change was based on that result.

Reproduce the bounded simulator selection from the worktree:

```sh
xcodegen generate --spec src/apps/ios/project.yml
xcodebuild -project src/apps/ios/Kria.xcodeproj -scheme Kria \
  -skipPackagePluginValidation -disableAutomaticPackageResolution \
  -destination 'platform=iOS Simulator,id=<available-iPhone-UUID>' \
  -derivedDataPath /tmp/cunda-recovery-dd CODE_SIGNING_ALLOWED=NO \
  DEBUG_INFORMATION_FORMAT= GCC_GENERATE_DEBUGGING_SYMBOLS=NO COMPILER_INDEX_STORE_ENABLE=NO \
  -only-testing:KriaTests/SavedEditorRecoveryTests \
  -only-testing:KriaTests/SavedDraftPreviewRangeTests \
  -only-testing:KriaTests/SlidePostRoutingTests \
  -only-testing:KriaTests/NativeEditorInteractionTests \
  -only-testing:KriaTests/NativeEditorRenderCompilerTests \
  -only-testing:KriaTests/NativeEditorSourcesTests \
  -only-testing:KriaTests/ChatDraftStagingTests test
```

Scratch evidence on the selected Mac: `/tmp/cunda-recovery-baseline-red.{log,xcresult}` and `/tmp/cunda-recovery-final.{log,xcresult}`. The green result contains retained preview frame attachments. The summary above is the durable safe evidence record; scratch paths are not portable artifacts.

## Limits and next action

This is native simulator and offline fixture evidence, not recovery of the original production project or a physical iPhone/Photos export. The installed client build and exact pinned production draft body remain unknown. No production write, retry, regeneration, deployment, account setting or issue was changed. Integrate and review together with the backend's recomputed `editor_draft_available` hint and matching legacy compatibility contract. The parent owns publication and original-project verification. No claim that every possible editor failure is prevented follows from this bounded recovery contract.
