# KRI-37 native editor validation

Status: verified subset approved for release on 2026-09-12; remaining parity and qualification are explicitly deferred in `TODOS.md`. Keep the existing native capability rollout gates closed. Passing the tests below does not establish complete rendering coverage or physical-device performance.

## Implemented paths

- Shared editor session across Chat/Editor navigation, project header, square preview, persistent tool rail, vertically packed timeline lanes, and floating project conversation.
- Pending text creation with a visible preview and keyboard; cancel/empty Done discards the pending item; completed creation adds one history transaction.
- Text editing, presets, font/alignment/color, outline/shadow controls, movement, combined scaling/rotation, and deterministic In/Out/Loop animation phases.
- Source asset resolution and caching; source-based composition; incremental parameter updates with stale-request protection.
- Generated server/Swift animation contracts, editor persistence, portable recipes, and cloud Skia/Pillow handling of the new text properties.
- Native positioned text/captions, basic audio levels and trims, camera pulses, supported transitions, media overlay placement/pop/dissolve, visual fills, and the shared motion command runtime.
- Authored legacy text timing, typewriter/word reveal, smooth reveal, ink reveal, and handwriting layout using the bundled cloud stroke glyph data.

## Remaining acceptance work

- Finish every editable look and adjustment. Golden Hour currently has restrictive source-format/canvas requirements; the other existing looks are not wired into the editor compiler.
- Finish carousel and supporting-card compilation.
- Match production audio normalization, music-bed looping/ducking, and beat-aligned music behavior.
- Finish editor compilation of specialized text layouts, including staggered slices, generated grouped/lyric text, subject occlusion, and theme transitions. Existing engine primitives alone do not establish editor coverage.
- Complete representative native/cloud frame and audio comparisons for all editable families and combinations, including backward seeks. Current layout/render fixtures cover specific paths, not the entire feature set.
- Complete Paper comparisons for all requested states and screen configurations, plus prompt-to-preview end-to-end verification against the live API.
- Measure sustained 30-fps playback, seek p95 at most 250 ms, 60-second export at most 120 seconds, memory, and thermals on both the requested iPhone 13 and a current iPhone. A connected iPhone 13 Pro was observed; it is not a completed measurement or the required device pair.
- Compatible additive source contracts were deployed in PR #1016 (see below). Complete qualification before opening native capabilities; this release does not change the production capability gates.

## Verification commands

```bash
KRIA_SIMULATOR_ID=<dedicated-simulator> make ios-verify
cd src/apps/ios/Packages/KriaMediaEngine && swift test
cd src/apps/api && .venv/bin/python -m pytest \
  tests/pipeline/test_text_animation_phases.py \
  tests/pipeline/test_authored_text_paint.py \
  tests/pipeline/test_authored_text_sequence.py \
  tests/pipeline/test_native_text_contract.py \
  tests/routes/test_native_timeline_sources.py \
  tests/kria/test_portable_motion.py \
  tests/kria/test_phone_text_motion.py
make verify-overlays
bash scripts/preship-check.sh
```

The shared timeline gate passed 209 web unit tests, 28 desktop browser checks,
42 mobile browser checks, and 511 backend tests. The media-engine suite passed
112 tests with four fixture-dependent skips; those transition and Golden Hour
fixture checks subsequently passed when run with production-generated assets.
The expanded production overlay check passed all 39 cases. All 14 editor UI
tests passed after the keyboard/layout corrections. The creation smoke test
failed once in the focused run, then passed on an isolated retry; the earlier
complete 23-test UI run also passed. The latest iOS unit build includes the
stale-preview playback guard and passes. These are simulator
and desktop verification results, not physical-device performance results.

The production overlay verification includes `authored_animation_phases.json`, covering every new entrance/exit choice and representative style combinations. Inspect both `report.json` and `montage.png`.

With Colima's default mounts, a worktree under `/private/tmp` may not appear inside Docker bind mounts. The production image can still be verified by creating a disposable container, copying `src/apps/api/tests/.` into `/app/tests`, running the same `app.cli.verify_overlays --fixtures` command, and copying its output back. Do not describe an empty-fixture run as a pass.

## iPhone source-preview compatibility fix (2026-09-11)

The production OpenAPI schema was checked directly: `TimelineClipOut` exposes
`signed_url` but does not yet expose `native_source`; `TimelineResponse` does not
expose `native_assets`. The initial client required the new field and therefore
rejected existing videos before preparing their composition.

The client now decodes legacy signed video-source URLs when `native_source` is
absent. Explicit null remains unavailable; image derivatives, non-HTTPS URLs,
and analysis proxies are not accepted as originals. A legacy response without a
baseline requires a fresh owned-variant read matching the document generation.
The resolver still rejects stale generations. This supports ordinary existing
video sources without deploying the new backend contract; additional asset lanes
still require that contract and complete native compiler support.

Preview failures now explain missing fonts, missing local originals, changed
versions, or unsupported effects without displaying signed URLs. The iOS unit
suite passed: 197 tests, one skipped, zero failures, including production-schema
compatibility and rejection cases. A signed Debug build using the production API was installed successfully over
`com.kria.app.dev` on the connected iPhone 13 Pro. The subsequent verification
with the affected real edit is recorded below.

## Physical-device preview failure follow-up (2026-09-11)

Device diagnostics identified two additional integration failures:

1. Talking-head variants intentionally expose no editable slot timeline. The
   original-source compiler received an empty document and raised
   `RecipeError.invalidTimeline`. The editor now uses the generation-bound,
   text-free composite base for this format, represents that immutable cut in
   the document, and retains the server's disabled timeline capability. This
   source contains the existing cut, B-roll, and audio; editable text still uses
   the same native composition and incremental renderer. The finished output
   cannot substitute for a missing base. Hydration preserves clean state and
   text undo/redo history created during preparation.
2. URLSession downloads were imported with their temporary `.tmp` extension.
   AVFoundation rejected valid H.264/AAC MP4 bytes with error `-11828`. The exact
   same file loaded successfully as `.mp4`. Imports now retain a media extension
   from Content-Type, a recognized source URL extension, or the response filename.
   This shared path covers source videos, audio, and media overlays.

Verification: 202 iOS tests, one skipped, zero failures. Regression tests reproduce
both failures, verify unchanged downloaded bytes become playable, reject finished
outputs as bases, and exercise local text updates through the live compositor.
On the connected iPhone 13 Pro, the affected real edit reached `preview-ready`,
rendered 1080×1920 frames at 0.1s, 1.0s, and 0.1s again, and advanced playback with
an AVPlayerItem in the ready state. A temporary authenticated test screen was
removed afterward. These checks establish the reported preview failure is fixed
for that edit; they do not satisfy the remaining performance or full-coverage
acceptance criteria above.

## Library coverage follow-up (2026-09-11)

The library audit found that cloud guided stories were not loading their public
slot timeline. The client now loads that timeline for both render destinations,
checks its generation, and preserves its revision. Actual guided edits expose
52 and 103 source segments rather than an empty filmstrip. Null or empty legacy
render IDs fall back to the render timestamp when restoring the document.

Music preparation now falls back to the track catalog when the variant has a
track ID but no preview URL. The affected music montage reached `preview-ready`
on the iPhone with its 13 cuts. Photo sources and SFX depend on the additive
backend source contract deployed by PR #1016. The Fly workflow passed exact-image
and fleet health verification; public `/health` returned 200 and production
OpenAPI exposed all three native contract fields. Native capability gates remain
unchanged.

Narrated visuals use persisted step assignments and timings, with the server's
short-source slowdown and caption-free narration audio. ImageIO can return a
non-null source for an MP4 while reporting zero images; image detection must
require a positive image count or video duration probing is skipped. A real MP4
regression covers this distinction. Legacy captions without IDs receive stable
local selection identities while retaining their original wire records.

Downloaded previews use a content-addressed cache under the app's Caches directory.
The project/generation index stores asset metadata without signed URLs; disk reads
verify the file fingerprint and path. Reopening an editor can reuse these files,
and repeated identical downloads do not create another copy. Device originals
remain in the project original store and are not deleted by preview caching.
Purged or changed cache files are misses and require the normal source download.

After freeing device storage, the updated build installed successfully. The narrated
edit reached `preview-ready` with eight cuts; the 103-cut mixed-media guided story
also compiled on device. Full-library diagnostics still found failures in three
other stories and older entries that return API conflicts. This is incomplete
library coverage, not acceptance of the whole editor.

A 52-cut story exposed an exact boundary mismatch: its persisted title ended at
44.867s while the assembled timeline ended at 44.866s. The compiler now bounds
rendered title/caption windows to the actual cut duration while preserving stored
editor timing. `testRoundedContainerEndDoesNotRejectTextAtFinalCut` covers both
lanes. Both affected 52-cut photo stories and the affected three-cut crossfade
story subsequently reached `preview-ready` on the physical iPhone. This verifies
recipe/composition preparation. Subsequent device checks decoded start, middle,
and end frames for all three stories.

Library cache lookup now uses the authenticated job ID when available. A library
open without a creation thread can receive a fresh local project ID, which must
not invalidate its previously downloaded sources. Generation remains in each key,
and different jobs retain distinct cache indexes. Regression:
`testLibraryCacheUsesJobIdentityAcrossLocalSessionDirectories`.

API responses for unavailable content plans and missing ready edits now receive
specific user-facing errors rather than the unsaved-edit conflict message.
Unknown 409/412 responses retain revision-conflict handling. The latest iOS unit
suite passed 213 tests with one skipped and zero failures. Nine focused native
composition, transition, and audio-mix tests also passed.

A real frame check exposed decoder exhaustion in a long photo story: allocating
one composition video track per cut produced 30 simultaneous track decoders.
The composer now reuses tracks for non-overlapping clips and preserves separate
tracks for overlapping transitions. A 40-cut regression uses one track; overlapping
crossfades use two. The affected phone story then decoded three 1080×1920 frames.
Warm reopen also verified 52 cache hits and zero downloads.

The initial device library sweep decoded three frames each for 10 of 12 prepared
previews. Individually probing the two failing source sets isolated VP9 (`vp09`)
decode failures. `NativeVP9Source` now prepares these originals locally using the
pinned libvpx 1.17.0 decoder and AVAssetWriter H.264/AAC. The original file remains
unchanged; a content-addressed disposable source cache preserves cut timestamps,
orientation, and audio. AVFoundation empty timing-marker samples are skipped.
The package bundles the decoder license and patent notices. The synthetic fixture
checks decoded frames, audio, duration, original preservation, cache reuse, image
bypass, and cancellation. The narrated eight-cut edit now decodes beginning,
middle, and end 1080×1920 frames on the physical phone. The 103-cut mixed-media
story still awaits completion of the same test after the phone unlocks.

A separate 39-cut guided story failed when opened through its project. The legacy
UUID-backed `TextLayer` adapter discarded non-UUID canonical text IDs; converting
back therefore dropped timing and styling and produced 172 full-duration layers.
`TextLayer.canonicalID` now survives the adapter and Codable round trip.
`testCanonicalTextIdentityPreservesTimedStoryLayersAcrossDraftBridge` verifies
all 172 identities, time windows, styles, and only one active layer at time zero.
The actual 39-cut story now opens and decodes three 1080×1920 frames on device.
Color emoji fallback is supported without permitting arbitrary primary-font
substitution. `NativeTextLayerStore` retains bitmaps only for the active time
window, with a 64 MiB budget; the long-story regression also covers backward seek.

The current regression runs pass 214 app tests (one skipped) and 120 media-engine
tests (four skipped), with zero failures. These are not a claim that all library
entries or all device acceptance gates have passed.

Nine legacy entries report unavailable content plans; two entries have no ready
edit. Inspecting the unavailable legacy records awaits the requested metadata-read
authorization. Ownership checks remain enforced; no production records were changed
by this preview follow-up.

Rotation verification must compare image content, not only successful frame decoding.
The compositor previously applied AVFoundation's top-left track matrix directly to
Core Image's bottom-left image coordinates, reversing quarter-turn rotations.
`coreImagePreferredTransform` reflects both coordinate domains before normalizing
bounds. `testQuarterTurnMetadataMatchesSystemPlaybackOrientation` creates asymmetric
video with 0/90/180/270-degree metadata and compares its dominant corner colors
against system playback. Both quarter-turn directions failed before the fix;
all four rotations and the eight composition regressions pass afterward.

## Text and voiceover interaction follow-up (2026-09-12)

Text movement and corner/pinch transforms use an immediate local layer. Alignment
feedback covers canvas centers/edges and horizontal/vertical orientation, with
hysteresis; rotation gently holds at cardinal angles and smoothly releases.
Enter and deletion control authored line breaks, including blank lines. The
`wrap_lines` property preserves that choice across native and cloud rendering;
legacy elements retain their existing wrapping policy until manually edited.

Preview playback activates the media audio session. Narrated, voiceover, and
narrated guided-story variants use their caption-free rendered narration mix,
with original visual-track audio muted and no duplicated selected music bed.
A 39-cut guided-story preview was rejected because its narration base was
44.633s while the visual timeline was 44.799s. Narration now ends naturally
rather than requiring it to span the complete visual timeline. The actual
project reached `preview-ready` on the connected iPhone after this correction.
The app was returned to normal operation after the diagnostic run.

This evidence verifies the reported interaction and preview regressions. It is
not acoustic qualification, complete native/cloud parity, or the required
two-device performance/accessibility acceptance matrix.
