# iOS device-only Release 1 rollout qualification

**Status:** IN PROGRESS — automated qualification; production hold
**Planned at:** `origin/main@b93328d88` (2026-10-02, after PR #1310)
**Implementation source:** PR #1310, “move production creation to on-device rendering”
**Test plan:** [`artifacts/025-ios-device-only-rollout-test-plan.md`](artifacts/025-ios-device-only-rollout-test-plan.md)

## Outcome

Move production creation to iPhone rendering without losing speech, captions,
existing playback, or recovery. The release is complete only when all admitted
creation formats pass the automated matrix and a signed TestFlight build proves
the real production journey from upload through `published` playback.

This is a qualification and operations plan. It does not redesign a renderer,
enable unrelated editor lanes, or perform the Release 2 Fly topology reduction.

## Why this plan exists

PR #1310 added the device-only admission and execution fences, but its production
runbook has one generic “phone-supported project” canary. That is not enough for
speech formats because their work is split differently:

- the server still analyzes a proxy, transcribes speech or voiceover, corrects
  captions, and compiles timed recipe layers;
- the iPhone downloads the recipe/assets, paints the captions, mixes audio, and
  exports the final video;
- recorded voiceover audio still uploads to the existing server contract and is
  downloaded to the device for the render;
- after cloud execution is disabled, a device refusal cannot silently fall back
  to a cloud render.

A single montage canary can therefore pass while Talking captions, narration
audio, caption editing, or a format gate is broken.

## Verified current state

| Area | State at the planned SHA | Evidence / consequence |
|---|---|---|
| Safe merge defaults | `IOS_DEVICE_ONLY_MODE=false`; `CLOUD_RENDER_EXECUTION_ENABLED=true`; protocol floor `2` | Merging PR #1310 does not itself cut over production. |
| Single-clip Talking | Phone compiler and sentence/word caption renderer exist; requires one portrait clip, at most 300 seconds, with `SUBTITLED_ARCHETYPE_ENABLED` and phone Subtitled enabled | First render must prove audible source speech and caption pixels on the device. |
| Multi-clip self-narrated Talking | Backend compiler/dispatch support exists, but `PHONE_TALKING_HEAD_RENDERING_ENABLED` defaults false and additionally needs self-narration plus `visualBlocks`, `visualVideos`, `alphaOverlay`, and `audioMix` | Do not assume KRI-136 being Done means the production gate is on. |
| Recorded voiceover montage | Phone compiler, voiceover asset grant, local narration mix, and simulator render test exist | Generic voiceover montage does not promise captions; test audio and absence of invented captions. |
| Recorded narrated format | Phone compiler produces narration audio and caption text layers | Test both audible narration and visible/timed captions. |
| Guided voiceover | Guided compiler supports a narration lane when the guided and shared narration gates plus `narrationAudio` hold | It needs its own canary because it uses a different compiler and Visuals path. |
| Caption editing | Production-shaped test settings say `PHONE_SUBTITLED_EDITOR_LANES_ENABLED` is on; KRI-241 remains In Progress for voice disappearing after an unsaved caption edit | Hard STOP unless the lane is disabled before cutover or KRI-241 is fixed and verified. Disabling it is an explicit product downgrade and must be recorded. |
| Existing cloud output | Ready cloud output remains readable/playable; cloud-mutating edits are rejected after device-only cutover | Canary both read compatibility and the typed edit refusal. |
| Cloud fallback | Native `.cloud` decisions become `needsAttention`; cloud task entry is fenced after the kill switch | Test the refusal/recovery UX. Never infer safety from an empty queue alone. |

Relevant Linear history: KRI-135, KRI-136, KRI-137, KRI-142, KRI-216,
KRI-232, and KRI-238 are Done. KRI-241 is the remaining known speech/editor
blocker for this rollout scope.

## Scope lock

### In scope

1. Re-derive and record the production phone feature profile without recording
   secret values.
2. Close the missing automated tests listed below.
3. Run the existing backend, iOS unit/UI, and native-render suites.
4. Run signed TestFlight canaries for every admitted speech/voiceover shape.
5. Update `docs/runbooks/ios-device-only-runtime.md` so these canaries are hard
   hold points before cloud execution is disabled.
6. Execute the Release 1 admission, drain, cloud-disable, and 24-hour acceptance
   steps only after every gate passes.

### Out of scope

- Release 2 process-group consolidation, VM downsizing, API scale-to-zero, and
  worker idle shutdown.
- New caption styles, new speech cleanup behavior, or new creative capabilities.
- Enabling PiP, reaction beats, SFX, video overlays, or other default-off Talking
  lanes merely because the base Talking canary passes.
- Changing retention, deleting old Jobs, or rewriting old cloud variants.
- Claiming zero-cloud processing. Analysis proxies and recorded voiceover audio
  remain server inputs.

## Release decisions

1. **All currently advertised formats must be tested.** The rollout is not
   allowed to rely on one representative montage.
2. **Multi-clip Talking is a separate gate.** Enable
   `PHONE_TALKING_HEAD_RENDERING_ENABLED` only after the missing native render
   test and a real-device canary pass. If it remains off, capabilities and chat
   must refuse it before a Job is created with actionable copy.
3. **Caption editing cannot ship with silent preview audio.** If production keeps
   the phone caption/editor lane on, KRI-241 must be fixed first. The emergency
   alternative is to turn the lane off and verify disabled controls/reasons; that
   alternative must be recorded as an accepted temporary product limitation.
4. **Cloud execution is the last functional switch.** Keep it on while device-only
   admission is proven and old cloud work drains. Turn it off only after every
   required canary reaches `published` with playable output.
5. **No silent fallback.** An unsupported recipe must remain on the device path as
   `needsAttention` or be rejected before Job creation. It must never start cloud
   FFmpeg, Skia, generation, or upload work.

## STOP conditions

Stop the rollout immediately if any of these is true:

- the App Store/TestFlight build does not send protocol `2` on ordinary, retry,
  refresh, and background-upload requests;
- KRI-241 reproduces while phone caption editing remains enabled;
- any required recipe capability is absent from the installed build or current
  `PHONE_RENDER_VERIFIED_FEATURES` profile;
- a required canary has silent speech/voiceover, missing captions, wrong caption
  language, stuck `awaiting_device`, failed completion, or unplayable output;
- a format advertised by `/creation-threads/capabilities` is later refused by
  dispatch or the worker;
- a post-cutover cloud Job is created, queued, or begins render work;
- any render queue is non-empty/unknown when the drain step requires zero;
- API/auth/project history/playback/account operations regress;
- the worker OOMs, repeatedly restarts, or queue age grows during acceptance.

## Execution plan

### Phase 0: fresh-session setup and drift check

1. Create a fresh worktree from current `origin/main`; do not reuse the shared
   checkout or the PR #1310 worktree.
2. Read this plan, its test-plan artifact, and
   `docs/runbooks/ios-device-only-runtime.md` in full.
3. Record the current Git SHA and compare every referenced file to the planned
   SHA. If admission, phone compilers, device completion, iOS rendering, or the
   runbook changed, update the audit tables before editing.
4. Re-read KRI-241 and any issues linked since this plan. Do not mark it resolved
   from code inspection; require a regression test and physical-device evidence.
5. Re-derive the production-shaped test profile from current Fly configuration.
   Record boolean names and capability names only, never secret values.

### Phase 1: close automated coverage gaps

1. **KRI-241 regression and fix.** Add a failing test to
   `src/apps/ios/Packages/KriaMediaEngine/Tests/KriaMediaEngineTests/LiveAudioMixTests.swift`
   using a video clip's own audio, not a still image plus SFX. Edit only caption
   or title text through `LivePreviewComposition.updateText`, then prove the same
   voice remains audible. Fix the root cause, cover text/style/volume fast paths,
   and add one full-rebuild control such as a trim.
2. **Multi-clip Talking render.** Extend
   `scripts/ios/phone-montage-render-e2e.py` with a real
   `compile_phone_subtitled_plan(cutaways=...)` case and add a matching test to
   `DeviceMontageRenderE2ETests.swift`. Assert continuous speaker audio, muted
   cutaway audio, cutaway pixels during its window, captions before/during/after
   the cutaway, and required capability negotiation.
3. **Forced-update UI.** Add a deterministic UI fixture for a typed 426 and an
   XCUITest proving the blocking update screen replaces every app route, remains
   scrollable at accessibility sizes, and exposes working App Store and support
   actions. Existing unit tests for the 426 parser/state remain the lower layer.
4. **Production profile contract.** Add or update a focused backend test that
   applies the current production-shaped settings, enables device-only admission,
   and asserts the picker, planner, dispatch gate, and worker agree for every
   format in the matrix. Include explicit rows for editor-lane on/off and
   multi-clip Talking on/off.
5. Keep current tests for admission, cloud task guards, completion identity,
   existing cloud reads, and typed mutation rejection. Do not duplicate them in
   a new test framework.

### Phase 2: automated qualification

Run the complete commands in the linked test plan. Required evidence:

- focused backend suites green;
- generated OpenAPI/Kria contract unchanged or intentionally regenerated;
- KriaMediaEngine package tests green;
- `make ios-verify` green with zero unexplained skips/flakes;
- generated native render cases green for generic voiceover, Subtitled sentence
  and word captions, narrated walkthrough, guided narration, and multi-clip
  Talking;
- `scripts/preship-check.sh` green after documentation and test changes.

Any skipped required render case is a failure. The retired montage cases in the
current fixture may remain skipped only when the unified planner owns equivalent
coverage and the test plan names that replacement.

### Phase 3: signed TestFlight qualification while admission is hybrid

1. Deploy the admission release with safe settings:
   `IOS_DEVICE_ONLY_MODE=false`, `CLOUD_RENDER_EXECUTION_ENABLED=true`, and
   protocol floor `2`.
2. Install the signed production/TestFlight build on the oldest supported test
   phone and one current phone.
3. Verify the protocol header directly. Exercise ordinary creation, token
   refresh, retry, and background upload.
4. Run the required canary matrix from the test plan using a designated canary
   account. Run the basic supported-project journey once on each of the four
   active mobile accounts.
5. Capture only private release evidence: UTC timestamps, app version/build,
   device/OS, Git SHA, flags/capability names, Job/variant/attempt IDs, final
   status, duration, memory, thermal state, and a pass/fail note. Never capture
   signed URLs, object paths, tokens, transcript text, or user identifiers.
6. Fix every failure and restart this phase from the first affected case. Do not
   waive a failed speech/audio/caption result because a montage passed.

### Phase 4: device-only admission and drain

1. Follow runbook step 2 to enable `IOS_DEVICE_ONLY_MODE=true` while cloud
   execution remains on for already-queued work.
2. Run the admission probes and repeat all required device canaries. Require an
   `ios_device_only_admission` receipt with protocol `2` for each admitted flow.
3. Verify an old cloud output still loads and plays. Verify a render-affecting
   mutation of an old cloud variant returns the expected typed refusal and does
   not alter the old output.
4. Run the broker and database drain audit exactly as documented. Unknown state
   is not zero.
5. Set `CLOUD_RENDER_EXECUTION_ENABLED=false` only after the canary matrix and
   drain audit are green.
6. Repeat one Subtitled and one recorded-voiceover canary. Confirm device recipes
   still reach `awaiting_device` and `published`, while an injected/controlled
   cloud publish attempt is terminalized before renderer work.

### Phase 5: acceptance and handoff

1. Monitor continuously for one hour, then hourly through 24 hours using the
   existing runbook signals.
2. Break metrics down by format/reason, not only total success rate: Subtitled,
   Talking Head, narrated, guided voiceover, generic voiceover, protocol update,
   unsupported recipe, upload, completion, and playback.
3. Require zero unexplained silent-audio, missing-caption, cloud-start, OOM, and
   stuck-device incidents.
4. Attach the sanitized release record to the private operational home. Update
   this plan and `plans/README.md` to DONE only after the 24-hour gate passes.
5. Release 2 topology work may begin only after this plan is DONE.

## Acceptance criteria

1. Every currently advertised format has a passing planner → dispatch → recipe
   contract test under the current production-shaped settings.
2. A physical iPhone completes single-clip Subtitled with audible speech and
   correctly timed captions through `published` playback.
3. A physical iPhone completes generic recorded-voiceover montage with audible
   voiceover and no invented caption promise.
4. A physical iPhone completes recorded narrated and guided voiceover cases with
   audible narration and the expected caption/text layers.
5. Multi-clip Talking either passes its automated/native/physical gates and is
   enabled, or is not advertised and is rejected before Job creation with
   actionable copy.
6. With caption editing enabled, unsaved and saved caption edits preserve voice
   and KRI-241 has a passing regression test plus device evidence.
7. Typed 426 presents the blocking update UI across every route; protocol-2
   traffic reaches normal auth.
8. Existing cloud output remains playable; forbidden legacy rerenders are typed,
   non-mutating refusals.
9. After cloud execution is disabled, no new cloud render starts and required
   device canaries still publish.
10. The 24-hour acceptance window has no STOP condition and the private evidence
    record contains every required field.

## Rollback ladder

Use the smallest rollback that restores a safe user experience:

1. Caption-editing-only regression: disable the phone caption/editor lane and
   confirm controls close with an explanation; keep first renders running.
2. Multi-clip Talking regression: disable
   `PHONE_TALKING_HEAD_RENDERING_ENABLED`; confirm the format is not advertised
   or is refused before Job creation.
3. One phone compiler regression: disable its dedicated phone format/voiceover
   gate where available and stop the rollout. Do not route it silently to cloud
   while device-only admission remains on.
4. General device failure: restore `CLOUD_RENDER_EXECUTION_ENABLED=true` first,
   verify old consumers are healthy, then set `IOS_DEVICE_ONLY_MODE=false`.
5. Capacity/topology regression: follow the runbook capacity restore before
   reopening cloud admission.

No rollback deletes Jobs, media, or device exports.

## Files to change in the implementation session

| File | Planned change |
|---|---|
| `src/apps/ios/Packages/KriaMediaEngine/Tests/KriaMediaEngineTests/LiveAudioMixTests.swift` | KRI-241 voice-preservation regression using a video clip's own audio. |
| `src/apps/ios/Packages/KriaMediaEngine/Sources/KriaMediaEngine/LivePreviewComposition.swift` | Likely KRI-241 fix location; confirm with the failing test before editing. |
| `scripts/ios/phone-montage-render-e2e.py` | Generate multi-clip Talking recipe/media expectations. |
| `src/apps/ios/Tests/KriaTests/DeviceMontageRenderE2ETests.swift` | Render and inspect multi-clip Talking audio, captions, and cutaways. |
| `src/apps/ios/Tests/KriaUITests/NativeUpdateRequiredUITests.swift` | New blocking-update journey test, if no equivalent file exists after drift check. |
| `src/apps/ios/Kria/KriaApp.swift` and UI fixture transport | Add only the deterministic test seam needed for the 426 UI test. |
| `src/apps/api/tests/tasks/test_phone_format_matrix.py` or a new focused sibling | Production-profile agreement across picker/planner/dispatch/worker. |
| `src/apps/api/tests/_prod_profile.py` | Re-derived current flags/capabilities with a dated note; no values/secrets. |
| `docs/runbooks/ios-device-only-runtime.md` | Replace the generic canary with the required matrix and evidence/STOP rules. |
| `plans/artifacts/025-ios-device-only-rollout-test-plan.md` | Update counts/commands if implementation changes test ownership. |

## Execution drift audit — 2026-10-02

The user requested reuse of `/Users/emirerben/Projects/nova-ios-device-rollout-test-plan`.
Preserved its existing plan edits and fast-forwarded from `b93328d88` to
`9d7f5fe71`. The intervening diff affects mobile CI, release metadata and package
metadata only; referenced admission, compilers, completion, iOS runtime and
runbook code had no drift.

The later `/ship` pass integrated `origin/main@1c3415df1`, including editor
deletion, keyboard-panel and partial-mute fixes. Final integration tests and
review evidence are recorded separately in the execution artifact below.

A read-only effective-settings audit confirmed API protocol floor 2, device-only
admission still off, cloud execution on, all phone format/narration gates on,
and the same 30 verified capabilities. Multi-clip Talking and phone caption
editor lanes are **already enabled**. The default-off table above describes
code defaults, not current production admission. Worker profile parity and
signed-device behavior must still be verified at the operational hold point.
KRI-241 remains In Progress; code/test changes alone do not close its device gate.

Execution evidence and remaining gates are recorded in
[`artifacts/025-ios-device-only-execution.md`](artifacts/025-ios-device-only-execution.md).

## Fresh-session kickoff

Use this as the first prompt in the fresh implementation session:

> Execute `plans/025-ios-device-only-rollout-qualification.md` from a fresh
> worktree based on current `origin/main`. Read the linked test plan and runtime
> runbook first. Start with the drift check and automated test gaps only. Do not
> mutate production or flip any rollout flag. KRI-241 is a hard gate while phone
> caption editing is enabled. Stop before TestFlight or production actions and
> report the exact automated evidence and remaining manual gates.
