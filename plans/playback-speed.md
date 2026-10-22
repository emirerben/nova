# Constant playback speed

Status: implemented and locally verified. Base: `1e119d4bf`
(origin/main, 2026-10-22). No deployment performed.

## Request and observed failure

Support slower/faster footage independently of moment selection, and combine both without
changing the selected action. A six-second source selection at 2x must play for three seconds;
at 0.5x it must play for twelve. Reset must restore six seconds, not select different footage.
The original M2 report supplies no media or incident identifier; KRI-689 documents the same
phone-creation limitation. Synthetic marked footage will establish timing/export fidelity,
not claim to reproduce the unavailable original incident.

Existing chat `patch_slots` changes duration with rate, but the guided phone compiler rejects
non-1 rates. Native controls change only rate (fixed output window). Cloud FFmpeg and the
native media engine already implement retiming. Web clip controls do not yet carry rates.

## Design and implementation order

1. Extend the existing guided phone compiler, capability checks, and source-time projection.
   Do not add a new processing service or rewrite original media.
2. Make native clip speed a source-preserving transaction using the existing speed inspector,
   saved timeline payload, undo, and preview compiler. Range 0.25–4x; 1x is normal/reset.
   Whole-edit changes use the existing chat selector, applying a constant rate to video clips;
   still photographs are not sped up. No ramps or optical-flow frame synthesis.
3. Make chat and manual changes agree and verify rendered media. Retain explicit limitations
   on formats whose speech/song clocks cannot yet be retimed safely.
4. Website editor integration is being scoped separately against the same saved contract.

`duration_s` remains OUTPUT duration. `in_s` remains SOURCE start. `playback_rate` remains
source seconds per output second. A speed-only command computes the new duration from the
selected source span and assigns an absolute rate. It never compounds rates, moves the start,
extends the selection, or changes legacy payload interpretation. Old projects require no
migration. Omitted rate remains legacy/1x; implicit source-span retimes need preserving.

Changing speed is a normal edit: original source assets are retained, the revision/hash changes,
existing save generation guards invalidate old results, and existing job retries and failure
reporting apply. Never label an old export as the newly saved edit.

## Six timeline decisions

1. **Duration owner:** server `build_guided_segments` plus guided revision normalization own
   ordered segment windows and transition overlaps. Native `NativeEditorInteraction.timelineProjection`
   is the paired client projection. A speed operation writes rate and output duration together;
   the inspector must not invent a separate timeline clock.
2. **Ripple:** following clips move by changed output duration after overlaps are clamped.
   Footage-bound editable text maps through source time. The first release refuses speed
   when media overlays, visuals, motion/camera, SFX, drawings or carousel inserts are
   present: these lanes do not yet have paired native/server source-time projection.
   Both the native interaction and server capability/Save guards enforce that boundary.
   Exact boundary points choose the following clip; interval ends choose the preceding clip.
   Source-speech captions must follow footage; narration and song lyrics retain their own
   audio clocks. Music keeps its pitch/rate and selected window, clamped if the video shortens;
   slowing footage does not silently extend the creator's chosen music interval.
   Do not open speed on speech/song-locked variants until this distinction is demonstrably safe.
   Use original transaction snapshots for previews; never repeatedly project a gesture's last
   rounded result or rewrite original media timestamps.
3. **Scrub bounds:** existing canonical projection duration clamps transport, ruler, pointer,
   keyboard and inverse mapping. Source time = source start + output offset * rate.
4. **Resize:** trim handles keep their existing source-selection meaning, now using rate to
   convert output distance to source distance. A speed change keeps both selected source edges.
   Reject invalid/non-finite rates, sub-0.1-second output and edits beyond the existing duration
   limit. Use the editor's 30 fps grid; cap overlaps against BOTH new adjacent lengths.
5. **Undo:** one control change/gesture snapshots the entire affected document once (clips and
   dependent lanes); undo and reset must restore the complete timing state. Unchanged assignments
   should not create a new edit. Reset means 1x, including originally implicit retimes.
6. **Parity:** paired source-window/timing vectors cover 0.25, 0.5, 1, 2 and 4x; repeated
   assignments, resets, trim+speed, repeated source occurrences, boundaries and overlaps.
   Export marked video and tones through real render paths. Measure duration/frame count,
   retained source markers and audio pitch; reject a wrong-speed control.

## Audio and quality

Original camera sound follows the clip rate with pitch preservation (FFmpeg `atempo`, native
explicit time-pitch processing). Existing mute/mix choices remain authoritative. Music and
voiceover never inherit footage speed. Low-frame-rate slow motion repeats available frames;
do not promise newly generated smooth motion. Lip-sync tied to a song remains locked.

## Acceptance and failure modes (before implementation)

- Same selected source start/end after 1x → 2x → 0.5x → 1x and repeated 2x.
- Manual, chat, saved revision, reopened preview and export agree on rate/duration.
- Selection alone, speed alone, and their combination pass independently.
- No double time mapping, one-frame holes, downstream drift, disappearing labels or
  speed applied to external music. Crossing intervals and exact cut boundaries remain correct.
- Unsupported formats/invalid values reject atomically, preserving the current edit.
- Legacy rates/holds and 1x exports retain prior behavior. Photos retain their duration.
- Real rendered files with/without audio pass measured duration and pitch checks. Marked
  source intervals identify what footage was retained; a wrong-speed/incorrect-window control fails.
- Run focused native/backend tests, `make verify-editor-timeline`, relevant lint/typechecks,
  and preship. Report unavailable simulator, provider, production-image or deployment evidence.

## Explicit remaining boundary

Initial natural-language creation has a separate planner that does not author requested rates.
Its unsupported-speed notice must stay until that full request-to-plan path is implemented and
verified. Existing post-creation chat operations already author rates; opening their compiler
does not by itself prove initial-creation speed support. No new wording heuristics or paid
prompt evaluation is authorized by this plan. No merge or deployment is part of this task.

## Implementation and verification record

- Reuse the existing speed control, timeline fields, editor Save, preview compiler and
  renderer. No new service, infrastructure deployment, media duplication or DB migration.
- `source_end_s` is the additive exact source anchor on timeline slots. Guided revisions
  mark these windows with `preserve_source_window`; legacy unmarked timing stays compatible.
  Snap marked output duration and boundaries to the existing 30 fps grid, ties to even.
  A final subframe hold absorbs rounding without consuming footage beyond the selection.
- Phone capabilities require verified `variableSpeed`, ordinary source-timed text and no
  unsupported clocks. Server Save also rejects stale clients and simultaneous speed/locked
  lane writes atomically, including resetting an already retimed clip.
- Original audio uses FFmpeg `atempo` / AVFoundation spectral pitch preservation. Background
  music keeps its own clock. No optical-flow smoothing, reverse or speed ramps are added.

Executed evidence on 2026-10-22 (offline, no paid model call):

- Focused backend regression suite: **493 passed, 1 existing strict xfail**. The expected
  failure is `test_crossfade_label_bars_do_not_overlap_in_time` in
  `tests/routes/test_phone_guided_timeline_commit.py`: adjacent clip labels can overlap during
  an existing crossfade. This remains a known limitation, not a passing visual-parity claim.
- Real authored phone chat → capability → operation → atomic Save → pinned recipe → reopen:
  **10 passed** in `tests/routes/test_authored_playback_speed.py`, including a non-frame-exact
  1.3x assignment and exact source anchors through reset. Guided chat/reopen is covered in
  `tests/agents/test_timeline_capability_context.py`; unsupported-clock atomicity has 29 cases
  in `tests/routes/test_phone_speed_safety.py`.
- Real FFmpeg exported media: **25 passed** in `tests/pipeline/test_phone_guided_speed.py`.
  The six-second synthetic source is blue outside 2..5s, then red/green/yellow in each selected
  second. Pixel checks find those markers at the expected retimed offsets. A real wrong-window
  export with the SAME output duration is rejected by the pixel oracle.

| Rate | Selected source | Export duration | Frames at 30 fps | 440 Hz source tone |
| --- | --- | --- | --- | --- |
| 0.5x | 2..5s | 6.0s | 180 | 440.012 Hz |
| 2x | 2..5s | 1.5s | 45 | 439.926 Hz |

Both exports also pass with original audio disabled (silence measured). Retained local
artifacts are under `/private/tmp/nova-speed-final-media/`; the table and fixture preserve
the safe evidence without committing media. This is local FFmpeg output, not production-image
parity or the unavailable original M2 media.

- Native media-engine XCTest: two focused capability/duration tests passed and one real
  AVFoundation export pitch test passed (440 Hz within ±25 Hz after 2x). Native output is
  generated by the macOS test runner; no physical iPhone/production claim. That test deletes
  its temporary export after checking it.
- Generated runtime/mobile contracts refreshed. Frontend TypeScript check passed.
- `make verify-editor-timeline` passed: 209 Jest tests, 28 desktop browser checks, 42 mobile
  browser checks, and 583 backend tests. Frontend changes are generated contract only.
- After adding the explicit 1x-reset source-bound guard, the affected compiler/Save/chat
  tests passed again: **212 passed**. Unmarked legacy 1x source fitting remains unchanged.
- Independent music-window regression: **42 passed** in `tests/pipeline/test_authored_timeline.py`;
  all five rates preserve background music rate and volume, shortening only its end as needed.
- Final focused simulator check: **274 XCTest passed, 0 failed, 0 skipped** on iPhone 17 Pro
  simulator / iOS 26.5. Command: `xcodebuild -quiet -project Kria.xcodeproj -scheme Kria
  -skipPackagePluginValidation -destination 'platform=iOS Simulator,name=iPhone 17 Pro'
  -only-testing:KriaTests/NativeEditorInteractionTests
  -only-testing:KriaTests/NativeEditorSessionTests
  -only-testing:KriaTests/GuidedLabelRebaseVectorTests test`. This includes half-frame,
  mixed/removed clips, transition boundaries, fractional no-op after reopen, minimum duration,
  photos, legacy cloud behavior, text projection, reset and undo.
- Full `make ios-verify` was attempted but stopped during setup under disk exhaustion; the
  final focused simulator suites above passed after recovery. The full iOS UI gate and a
  physical-device acceptance pass remain unverified. Host storage exhaustion also temporarily
  blocked npm; only this
  task's generated build cache was removed, and matching locked frontend dependencies were
  cloned locally to continue checks.
- The KRI-689 CI journey now selects the speed compiler/Save/source-projection regressions
  and named native boundary tests, including the previously unmapped `reframe.py` path.
  The journey evidence-policy checks passed (6 tests). Implementation source is commit
  `afd5ef22a`; the following CI/docs-only commit does not change that tested source.

The original M2 report supplied no clip or saved job. Its specific incident replay, initial
creation planner speed, website speed controls, timed-effect/speech integration and rollout
remain open under the KRI-689 feature boundary. This change establishes the narrower existing
phone-editor speed capability. Do not describe the full original creation request as resolved.
