# Saved editor recovery after render failure

Acceptance before implementation: a cold-open runtime-v2 failed video project with a complete current saved editor snapshot offers Open saved edit and the Editor switch. Opening uses the existing read-only editor loader and exact persisted lanes. The render remains failed; no planning, retry, dispatch or save occurs on open. A no-op chat reply and reopening do not remove this access or erase same-job local changes. Failed first renders without a saved snapshot, reference-only drafts, mismatched item/job/variant/generation, unknown format and slides do not expose video recovery. Real missing media and insufficient source windows still fail honestly.

Production read-only observation on 2026-11-06: the incident job's authored video slot requested 1.933333s against a stored source-duration hint of 1.92s, in_s 0, no source_end_s, playback_rate null, unlocked, not removed. Job and variant remained failed with editor_render_recovery from November 5 20:04 UTC, before the source-window patch deployed. The variant pinned a v2 saved draft reference, while the original phone export remained recorded published. The November 6 white-text turn was a no-op and dispatched nothing. Approved admin diagnostics exposed only the pinned draft ID, not its body or base generation, and stripped the device recipe; current snapshot validity and exact recipe end/hold remain unverified. No production state was changed.

This change must apply through shared native contracts, with no project, account or title exceptions. Opening alone does not establish current-project recovery: legacy source-window preview/Save compatibility and physical iPhone Save/reopen/Photos export require independent evidence.

## Implemented contract

The full native creation projection now retains `editor_state_version`, `editor_draft` and `editor_draft_available`. Recovery requires a full snapshot matching the active item, job and selected variant, plus matching snapshot payload/base generation. A published-source generation alias needs the server's recomputed availability attestation; older servers retain the direct-generation path. Reference-only snapshots, malformed bodies, missing identities and unknown/slides formats grant no recovery. The chat offers Open saved edit and its Editor switch without relabeling the failed render.

Opening uses the existing editor loader unchanged for lane hydration. It arms the existing explicit Retry render flow only for a matching persisted failed saved edit. Ordinary unchanged Save still does nothing. Explicit retry resends the timeline through normal commit validation and generation fences; opening and Refresh dispatch nothing. Concurrent local edits survive and a second tap during the commit does not duplicate it.

The shared legacy numeric helper admits only a positive tail whose nearest 30fps ties-to-even duration equals authored duration within one microsecond, with at most half a frame plus one microsecond of padding. Strict fit/reanchor runs first. Native compilation uses the resolved original's duration for this compatibility exception; missing original duration grants no exception. Exact endpoints, explicit native spans, real freezes and non-grid overruns retain their existing behavior. An authored fractional-frame duration retains its exact clock before sources load; this does not authorize source padding. Its neighboring durations and overlaps retain their continuous authored values. Source admission runs later against the resolved original, independently of saved duration hints. Absent and JSON-null endpoints share semantics; malformed non-null endpoints are refused. The stored lanes are not rewritten.

## Executed evidence

Base: `3d35555837fccd8d06e8c1b7d627cecb7ce13f98`.

- Initial simulator attempts stopped at package cloning and object emission because disk was full. These were infrastructure failures, not a red behavioral result.
- After capacity returned, original production source files from the base were temporarily restored in this isolated worktree. The new tests using only base APIs executed: 9 tests, 15 failed assertions including one thrown `invalidTimeline`. Observed failures included lost saved-draft fields, absent unchanged retry, 0.993103448275862 playback rate, no padding, 6.933 versus 6.933333333 timeline end, and mixed-timeline refusal. Implementation and test bytes were restored automatically afterward.
- Initial candidate (superseded by the review correction below): **232 simulator tests passed**, 0 failures, on iPhone 17 Pro / iOS 26.5. Selection: SavedEditorRecoveryTests (9), SavedDraftPreviewRangeTests (5), SlidePostRoutingTests (12), NativeEditorInteractionTests (33), NativeEditorRenderCompilerTests, NativeEditorSourcesTests (25), ChatDraftStagingTests (75). This includes actual generated 1.92s video plus image timelines, decoded frame attachments at the padded tail, no-op reopening, explicit retry commit payload, source identity guards and concurrent edit retention.
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

## Combined local integration

The supplied backend patch from parent head `ebb71d002` was reconstructed and all seven target Git blob hashes were checked against the supplied diff before application. `git apply --check` passed. Its 617 socket-blocked offline tests and independent review are parent-provided evidence at that backend head; they are separate from the 232 native simulator checks above. Local integration introduces no changes to native implementation or selected test source. No simulator rebuild is needed for that backend/docs-only addition. Production diagnosis wording now distinguishes stored duration hints from a verified original receipt.

Current main at integration intake is `31477c8e9b0eabf5f143d4b506b9543a7c17f44c`. Its changed paths since the common base do not textually overlap this candidate. Native application source equality to tested commit `4a001e44c34aca09dda34e120c866438ae0341aa` is checked independently after local integration. The prior native results do not establish current-main backend contract behavior; the full 617-test suite and DB-backed/physical-device gates still need revision-bound integration verification before publication.

## Limits and next action

This is native simulator and offline fixture evidence, not recovery of the original production project or a physical iPhone/Photos export. The installed client build and exact pinned production draft body remain unknown. No production write, retry, regeneration, deployment, account setting or issue was changed. Integrate and review together with the backend's recomputed `editor_draft_available` hint and matching legacy compatibility contract. The parent owns publication and original-project verification. No claim that every possible editor failure is prevented follows from this bounded recovery contract.

Post-integration static checks: changed backend Python sources compile without importing application code; scoped Ruff lint and format checks pass; `git diff --check` passes; journey mapping reports no unmapped paths; release-metadata ownership passes. No reliability gate maps to the changed paths. Full preship is not claimed: a revision-bound PR request-evidence record and current-main backend runtime test rerun remain pending, as do database-backed Save/reopen and physical-device/rollout verification. No PR, issue, push or deployment was created.

The native application tree hash is `e96b315aada019529d88078feb4bfc78bb8ec9c9` both at tested native commit `4a001e44c34aca09dda34e120c866438ae0341aa` and after rebasing the combined candidate onto `31477c8e9b0eabf5f143d4b506b9543a7c17f44c`. The native package sources and all seven selected simulator test files are byte-identical too. The 232-test native result therefore retains its source binding; this equality does not establish current-main backend behavior or original-project recovery.


## Review correction — 2026-11-06

Independent review found three gaps in the initial candidate: JSON null skipped
legacy eligibility, stale duration hints chose the projection clock, and a recovered
tail snapped neighboring legacy durations/overlaps onto the frame grid. Tests were
added before the correction. `/tmp/cunda-review-red.{log,xcresult}` executed eight
tests with 25 failed assertions, including 0.993103x/0.982759x rates, missing holds,
and altered off-grid timing. All prior evidence and the failed exploratory fixture
record above remain preserved; the earlier 232-pass result did not cover these gaps.

The correction removes duration hints from clock selection. An authored fractional
30fps duration keeps its value and a continuous timeline cursor, without rounding
neighbors or transitions. This clock selection grants no source compatibility.
Compilation uses the bound original to restore the authored rate/span, tries strict
fit/reanchor first, and only then admits the existing half-frame tail exception.
Missing original measurements cannot grant padding. Missing/null source endpoints
are equivalent; invalid non-null endpoints fail instead of becoming legacy slots.
Stored authoring is unchanged; no endpoint is injected.

The first corrected neighboring run caught an overly broad measured-duration
requirement on an ordinary strict-fit fixture. That restriction was narrowed while
keeping original measurement mandatory for inferred padding. The intermediate
failure remains in `/tmp/cunda-review-green1.{log,xcresult}`.

Final bounded simulator result: **238 tests passed**, zero failures, at
`/tmp/cunda-review-green2.{log,xcresult}` (same seven classes as above;
SavedDraftPreviewRangeTests now has 11 tests). This covers actual measured 1.92s
video, shorter/longer/missing/null saved hints, original 1.90s refusal and 2.0s strict
fit, missing original measurement, null and malformed endpoints, an off-grid 1.91s
neighbor with 0.157s overlap, and independent text/audio times. No private media or
production project was accessed.

`Tests/Fixtures/saved_editor_legacy_wire.json` is generated from actual backend
`prepare_authored_editor_commit` and `editor_sections` output with JSON serialization.
The backend regression compares that fixture byte-for-value, checks null endpoints,
and verifies exact video/transition/text clocks. Native reopens the same bundled
snapshot through DraftSnapshot and compiles it, with a separate clip-audio lane to
verify its off-grid timing too. This is an in-memory Save preparation and serialized
snapshot contract test, not a database-backed Save/reopen transaction.

Only a backend test was added; backend production files are unchanged. The new
serialization test plus existing frame-padding suite passed **38 offline tests**
(`/tmp/cunda-review-backend.log`), with connect/connect_ex/create_connection blocked
before any app import and a clean environment containing dummy credentials. Scoped
Ruff lint/format and diff checks pass. Parent separately reported 617 backend tests
and green backend preship on base `31477c8`; those were not duplicated on Mac.

Publication remains unauthorized. Physical iPhone recovery, pinned private draft
and immutable original inspection, database Save/reopen, Photos export and rollout
remain unverified. Speed draft #2125 is separate and unmerged: integration must
reconcile NativeEditorSession/ChatWorkspace changes and check conditional-304 and
late-response recovery behavior when that branch is combined. No production or
settings changes, push, merge, source upload or issue update occurred.

The 238-pass source binding is native application tree
`7364702402611518e321c9b8bbbe92e6c3880df2`, iOS tests/fixtures tree
`00693a2d20a8921b1ca1c948a0d80af778990840`, and native packages tree
`a8ae4da503f219d9e6d1573556d68dcd7eb64af5`. Only the evidence document and
backend test assertions/formatting changed after that native run. The new backend
assertions were included in the final 38-pass offline execution.
