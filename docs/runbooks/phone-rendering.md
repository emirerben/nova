# Phone rendering — KRI-29 work in progress

Phone rendering is an **account-scoped experimental pilot**, not a completed
KRI-29 implementation or a general rollout. On 2026-09-11 the user requested
enabling the currently supported path for their own account after the physical
pilot. Full style parity, long exports, thermal and recovery qualification remain
open in the [coverage ledger](../reviews/kri-29/coverage.md).

## Account pilot configuration

Deploy the account-gating backend before enabling it. Set `PHONE_RENDER_USER_IDS`
to a JSON array containing only the explicitly enrolled user UUIDs, and set
`PHONE_RENDER_VERIFIED_FEATURES` to the selected capability list before setting
`PHONE_RENDERING_ENABLED=true`. An empty cohort means global eligibility when
enabled; never use an empty cohort for this pilot. All flags default off/empty.
The same configuration must reach API and worker process groups.

Capabilities, proxy reservations, job dispatch, editor revision compilation and
new export reservations enforce the cohort. Recipe compilation also checks the
capability list and rejects giant-title handwriting because its measured export
cost is too high. Unsupported edits fail without rendering analysis proxies or
uploading originals. Existing cloud projects keep their current path; the local
path starts with new, explicitly consented analysis-proxy uploads.

Rollback: set `PHONE_RENDERING_ENABLED=false`. This blocks new attempts while
preserving published outputs and allowing already-reserved exports to finalize.
The pilot does not satisfy or remove the remaining KRI-29 release gates.

## Implemented foundations

- `KriaMediaEngine/SourceAssetStore.swift` binds opaque server media IDs to
  fingerprint-verified originals in app-managed project storage. Proxies,
  traversal paths, and escaping symlinks cannot resolve as originals.
- `app/kria/render_assets.py` and `KriaMediaEngine/RenderAssets.swift` define the
  portable asset manifest: local media IDs plus exact fingerprints, or library
  catalog IDs plus generations and fingerprints. The native resolver checks the
  recipe identity against the local original binding. The library cache verifies
  copied bytes before installation, detects corruption, and rejects original
  sources. Neither manifest contains download URLs or storage paths. V2 device
  requests carry this manifest in their digest; the coordinator's source resolver
  uses it before composition. The owner/revision-fenced `device-render/assets`
  route grants short-lived generation-pinned downloads only for published,
  ready music/SFX named in that recipe. It verifies the bytes and rechecks the
  owner, revision, and catalog after verification. The native authorized resolver
  downloads through a separate ephemeral session and verifies cache installation.
  Existing renderer fonts ship in the app bundle with their license files and
  enter the same verified cache only when the recipe's filename/hash matches.
  Shared guided planning builds original/font manifests; music/SFX and overlay
  planning/catalog integration remain outstanding. V1 projects retain their
  original migration and decoding path.
- The native writer always emits AAC, including bounded silent PCM chunks for
  edits without source audio. A real export test decodes that AAC to check
  silence and duration alongside preview/export frame parity. The V2 publish
  verifier rejects missing audio. Native timelines are capped at 30 minutes
  before conversion to Core Media time values; V2 server validation shares the
  duration/source-start budget.
- V2 `text_layers` carries independent time windows and positioned text runs:
  exact font identity, size, baseline, tracking, fill, outline, and rotation.
  `PortableTextDrawing.swift` draws these through the shared compositor, rejects
  missing glyphs/font substitution, and caps prepared text bitmaps at 64 MiB.
  The lane accepts shaped text with static, fade, scale, slide, pop, and bounce
  transforms using complete normalized motion parameters. Native whole-layer
  sampling matches 52 cases per motion version captured from the real cloud drawing dispatcher,
  including exit fades; animated fade runs through preview and H.264 export.
  Ordered colored blur layers provide shadows/glow behind each run; a standard
  shadow probe is visually close to Skia (not a full style-parity gate). Stroke
  width is the full centered width, twice the cloud `stroke_px` value. Reveal
  effects have staged coverage described below. Legacy unshaped runs now carry exact
  glyph IDs/positions from Skia and validate them against the bound font; the
  phone does not reshape those runs. `portable_text_layout.py` compiles the
  supported base text styles with production wrapping/anchor helpers and exact
  font assets. It rejects unimplemented specialized treatments;
  the full scene planner remains incomplete. Linear gradients use resolved endpoints/stops
  and explicit sRGB colors, clipped to glyphs; a reference probe caught and
  fixed device-RGB conversion adding green to a red/blue gradient. A Latin font probe matched cloud ink bounds and caught/fixed
  stroke/fill ordering; this is not full typography parity. The native suite
  verifies the lane's time window in actual preview and H.264 export. The new
  `positionedText` capability remains outside the default supported set.
- `TextMotionTiming.swift` mirrors the cloud's normalized v2 phase grid and
  smooth-type reveal math. `phone_text_motion_v2.json` covers all 17 effect timing
  rules plus multilingual, emoji, empty-line, ordering, and speed cases. This
  verifies timing calculations, not the unimplemented typography/effect pixels.
- `Composition.swift`, `RecipeVideoCompositor.swift`, and `RecipeWriter.swift`
  share timeline interpretation between preview and H.264/AAC export. Current
  coverage is basic composition, transforms, variable speed, explicit overlapping
  crossfades, basic timed text, basic audio mixing, and photo-only timelines.
  `StillTimelineClock.swift` creates a one-frame on-device clock for photo-only
  composition and retains it with the player item/asset. Photos are decoded
  through ImageIO before AVFoundation video probing, with EXIF orientation applied.
  Unsupported effects must
  fail closed. This is not cloud-renderer parity.
- `DeviceRenderCoordinator.swift` persists request identity and export/upload
  recovery state. A newer request fences a late export or upload completion.
  A completed local file can retry upload without rendering again.
- `app/kria/device_render.py` defines immutable recipe identity and typed upload
  requests. `app/services/device_render.py` pins one recipe per revision in private
  job state. The guarded guided-story worker now calls it after shared planning
  and stops before rendering. Publication rechecks ownership, generation,
  approval, and source bindings; redelivery preserves the issued request.
- `app/routes/device_render.py` authenticates job ownership, checks current item
  and ownership epoch under locks, reserves create-only uploads with cleanup
  receipts, and verifies generation, checksum, H.264, geometry, frame rate, pixel
  format, rotation, AAC when present/required, and duration before publication.
  It rechecks the current recipe and phase after downloading/probing without DB
  locks. Outstanding reserved exports may complete after the kill switch turns
  off; new reservations are blocked.
- `Kria/Core/DeviceRendering.swift` implements the native API adapter and isolated
  storage-upload session. `DeviceRenderSessions` owns coordinators across screen
  navigation; chat polls the device destination and starts only schema versions
  and capabilities explicitly advertised as verified by the server. Sign-out
  cancels its sessions. The status card distinguishes preparation, rendering,
  local availability, syncing, and synced output, with local playback/sharing
  and sync retry. Supported editor saves issue a new device recipe revision. Capabilities remain
  disabled until device verification.
- Project proxy uploads now carry immutable original fingerprint, duration,
  geometry, orientation, and audio provenance through reservation and recovery.
  Migration 0105 stores the binding before a signed PUT is issued. Registration
  verifies the proxy generation, duration, geometry, frame rate, rotation, and
  source-audio presence. Reserved proxy IDs remain distinguishable after the
  reservation is consumed. The job constructor accepts proxy footage only for
  gated content-plan jobs with exact private bindings; dispatch also requires
  an approved guided plan. Every other cloud path still rejects proxies.
  The footage picker chooses analysis proxies only when the server advertises
  the required verified native capabilities. Its consent explains that proxy
  video/audio uploads for analysis and finished exports sync after rendering.
  Existing phone sources lock the destination: rollback pauses attachment,
  never converts it to original upload. Existing cloud projects retain their
  original-upload consent. Mixed/unknown sources and unsupported attachment
  roles are blocked; visual-pool/narration proxy support remains outstanding.
- `services/phone_sources.py` resolves selected server-owned upload receipts
  into immutable original bindings, rejects mixed/missing/conflicting sources,
  and requires each approved moment's media ID, path, and generation to match.
  `_phone_sources_v1` is private at every public assembly nesting level.
  `pipeline/phone_guided_plan.py` projects the shared guided execution plan into
  V2 original assets, exact contiguous video trims, audio level, and supported
  text layers. Unsupported media treatments, transitions, and editor lanes
  reject instead of disappearing. Supported plans enter `awaiting_device`
  instead of a cloud-render state; the chat status card consumes this state.

## Native handwriting coverage

The portable text contract carries authored handwriting centerlines and per-stroke
progress windows. The phone paints partial paths locally, with round caps, outline,
gradient, glow, shadow, and anchor rotation; it reuses the settled bitmap after
the reveal. Handwriting does not substitute ordinary font glyphs.
`phone_handwriting.json` records paths from the real cloud draw function at five
progress values. Native tests compare those coordinates and partial-frame ink
coverage. Both legacy and authored timing fixtures include handwriting. Physical
device performance and full preview/export visual parity remain release gates.

## Remaining implementation gates

1. Extend local bindings to creator visual-pool and narration
   assets, with separately consented cloud recovery and source relinking.
2. Separate shared cloud analysis/planning from media processing for every
   creator style. Phone jobs must persist a portable recipe and stop before any
   cloud effect generation or final encode. Preserve approval/revision fences
   and worker-redelivery idempotency.
3. Replace the limited V1 recipe with a negotiated complete contract. Inventory
   creator-accessible effects at the agreed baseline and build a coverage matrix
   including combinations. Port typography/fonts, captions, lyrics, narration,
   SFX, overlays, camera effects, motion scenes, and carousel treatments. Do not
   silently map those treatments to basic text or cuts.
4. Connect chat creation and editor saves to the durable coordinator, including
   local playback/sharing, distinct syncing state, cancellation, relinking,
   retry, and separately consented cloud fallback.
5. Integrate final publication with poster generation and existing finalization
   side effects. Audit account deletion, retention, expired reservation renewal,
   original-audio presence validation, and cross-device behavior.
6. Produce cloud reference fixtures and compare preview/export typography,
   layout, compositing, timing, and audio. Test interruption and re-edits across
   the full matrix. Run the complete repo gates before opening a release PR.
7. Measure iPhone 13 and a current iPhone: sustained 30-fps preview, seek p95
   <=250 ms, 60-second exports <=120 seconds, peak memory and thermal behavior,
   with no crashes or critical thermal state. Enable only verified groups;
   KRI-29 remains incomplete until every agreed group passes.

## Verification commands

```sh
cd src/apps/api
.venv/bin/pytest tests/test_device_render.py tests/routes/test_device_render.py \
  tests/test_mobile_auth_contract.py tests/services/test_public_assembly_plan.py -q
.venv/bin/python -m app.cli.kria_contracts --check
```

```sh
cd src/apps/ios/Packages/KriaMediaEngine
swift test
```

From the worktree root: `make ios-verify` and `bash scripts/preship-check.sh`.
Simulator and synthetic fixture checks do not satisfy the physical-device or
cloud-reference parity gates.

### Native editor revision integration (staged)

Editor saves retain the existing timeline behavior. The canonical guided runtime
plan owns ordered moments and total duration; `NativeEditorInteraction` projects
that document for scrub bounds and timeline geometry. This integration adds no
insertion, ripple, resizing, or overlap policy: all lane timing remains in the
existing revision, continuous music stays continuous, and the current trim limits
and frame rounding apply. Save acknowledgement preserves follow-up edits and
rebases their undo snapshots; individual gestures remain one undo step.

The phone compiler consumes that same runtime plan. Supported legacy text-only
saves replace only the editable text lane. Compilation and source binding checks
must succeed before either the desired variant or its next device receipt changes.
`test_phone_editor_commit.py` covers revision supersession and atomic failure;
`phone_text_transforms_{v2,legacy}.json` and `PortableTextTests` cover cloud timing
references and real native preview/export. All unimplemented lanes still fail
closed, and the rollout remains disabled pending the complete parity/device gates.


### Relinking local originals

The device render status panel offers **Find original files** after a stopped or
failed local attempt. Selecting a file copies it into app-managed storage and
checks its complete SHA-256 and byte count against the current immutable recipe.
Only an exact match replaces the media binding; mismatches and selections for a
superseded revision leave existing bindings intact. No upload is performed by
relinking. Once all originals are present, retry uses fresh rollout capabilities.
This also supports Gallery/cross-device projects that lack a local source mapping.
Source manifest writes are serialized; neither proxy paths nor symlink escapes
can become original bindings. Tests: `SourceAssetStoreTests` and
`DeviceRenderSessionTests.testRelinkRequiresExactBytesAndCurrentRequest`.


### Ink Reveal parity (staged)

Ink Reveal is a native crop of the fully styled text block. Its versioned text
contract carries the production block bounds, including stroke, shadow, and glow
bleed. The crop rotates with the text; legacy delay/duration compression and v2
speed, easing, intensity, and exit fade use the cloud timing equations. This adds
no timeline duration or ripple changes. Actual cloud clip captures cover rotated
and glowing text, the shared timing fixtures include both motion versions, and
`PortableTextTests.testInkRevealClipsRotatedTextInPreviewAndExport` verifies the
partial and settled phases through real preview and H.264/AAC export. This does
not enable the separate Handwriting stroke effect or any unverified capability.

### Typewriter and Stream In parity (staged)

The phone paints discrete prefixes inside the full text's resolved line geometry.
Legacy typewriter schedules and scalar slicing, v2 grapheme slicing and intensity,
word streaming, cursor styles/blinks, and exit fades use cloud-compatible timing.
The recipe carries cursor offsets for every prefix; blanks retain their layout
without passing Skia's empty-paragraph sentinel coordinates to the device.
One partial bitmap is cached, and settled frames reuse the complete bitmap.
`phone_text_reveal.json` pins 528 real cloud dispatcher samples;
`phone_reveal_layout.json` pins drawing positions across three anchors and both
shaping modes. `DiscreteRevealTests` checks rotated raster frames, memory rejection,
and text windows through actual native preview and MP4 export. These additions do
not enable rollout; full style/combinations and physical-device gates still apply.

### Smooth Type parity (staged)

Smooth Type paints shaped text into separate line bitmaps and masks each before
compositing. The compiler resolves clipping bounds with stroke/shadow/glow bleed,
blank-line timing, and first-strong Unicode direction. Forward, reverse, and
center-out masks rotate around the text anchor. The compositor applies the
versioned entrance blur, translation, opacity, and exit timing; legacy payloads
without v2 motion settle immediately as shaped text, matching cloud behavior.
The total bitmap budget includes every retained line plus settled and partial
composites. `SmoothRevealTests` compares masks to real cloud drawing calls and
exercises rotated text through native preview and H.264 export in all three
orders. This remains staged: the remaining effects/styles and physical-device
performance and visual gates are still required before rollout.

### Staggered Slice parity (staged)

The compiler carries individually positioned grapheme runs and their logical
line/index mapping across wrapped rows. Native timing preserves the first-word,
remainder, and subsequent-line stages, short legacy window compression, and v2
speed/intensity. Each glyph gets its own opacity, vertical offset, and pivoted
rotation before the settled text takes over. The production partial branch uses
per-glyph rotation; only its settled branch applies the overlay rotation. The
phone preserves that distinction and does not add the common text exit fade.
`phone_staggered_timing.json` covers 105 timing cases, and
`StaggeredPainterTests` compares actual cloud draw calls and exercises both
motion versions through native preview and H.264 export. Glyph and composite
bitmaps share the bounded text-memory budget. This remains disabled for rollout
until the complete capability matrix and physical-device gates pass.

### Native dissolve coverage (rollout disabled)

`DissolveTiming` matches the cloud's exit window and seeded particle alpha.
`DissolveNoise` ports Skia's one-octave noise and text displacement map; its
fixture samples 960 noise pixels and 320 merged-map pixels across a 1080×1920
canvas. Native samples match within one 8-bit channel value. Production's
normalized color matrix receives an offset of `-2 * 255`, so the coarse red
and green channels clamp to zero; the frequency-1 fine field is neutral at
integer lattice coordinates. Preserve these observed semantics for parity.
Skia attribution is bundled in `Kria/Resources/Skia-LICENSE.txt`.
The recipe carries an explicit seed derived from the combined cloud overlay
index across text, context, and narration lanes. Media-card dissolve remains
a separate outstanding treatment.

`NativeDissolveWarp` now retains a bounded map and runs nearest-neighbor
displacement through a Metal Core Image kernel. `DissolveWarpTests` compares
1,152 actual cloud pixel samples across three seeds and four displacement
scales, including clipped edges, with exact channel equality. Shader creation failure remains unsupported.

`NativeDissolveRenderer` now composes the canvas-centered growth and seeded
particle mask. `DissolveCompositionTests` checks all 4,300,800 alpha pixels over
14 frames, with mean absolute error ≤1 byte and total alpha within 3% of Skia;
the observed worst full-frame error is 0.48 byte. Diagnostic PNGs confirmed
orientation and breakup. Unlike its nominal timing helper, the cloud image
filter does not apply intermediate paint opacity: only the zero-alpha early
return clears the last frame. Native composition preserves that behavior.
The map and particle field retain eight bytes per canvas pixel within the
caller-supplied budget. The integrated text painter reserves twenty bytes per canvas pixel for retained
maps, text, and intermediate composition. `DissolvePainterTests` verifies its
time window and exit breakup through native preview and H.264 export.
Rollout and physical-device verification remain outstanding.
Generate Python fixtures with `PYTHONPATH=.` so the shared editable environment
cannot silently import another checkout's renderer.


### Native karaoke and slide-in coverage (rollout disabled)

`karaoke-line` carries fixed-size word glyph runs, independently normalized local
start times, and a highlight color. Each word switches color at its start time
and remains highlighted; out-of-order starts are preserved. Blank timings use
the production raw-text static fallback. The compiler preserves per-word wrap,
tracking, outline, and shadows; production ignores shaping, gradients, and motion
for this handler. `phone_karaoke_layout.json` captures real production positions
and colors at timestamp boundaries across all horizontal anchors.
`NativeKaraokePainter` retains two bounded word images and composes them in draw
order, with both retained images and composite surfaces charged to the shared
text budget. Native preview/export tests include backward seeking, time-window
clipping, and independent word color changes. Both contract validators reject
missing, mismatched, or invalid timings.

`slide-in` currently renders as static text in the production Skia dispatcher.
The phone preserves that actual behavior, including ignoring motion and exit
fades. A cloud full-frame comparison and native timing test pin this behavior.


### Lyric and sequence fades (rollout disabled)

`TextFadeEnvelope` carries the production lyric head/tail durations and square
or square-root tail curve. Short windows clamp the head first, then the tail;
legacy lyric defaults remain 150/250 ms. Sequence envelopes add only a tail on
top of their entrance and motion exit alpha. A 72-case fixture captures alpha
from the actual production dispatcher across both curves, short/long windows,
and legacy/v2 motion; native tests match to 1e-9. Preview/H.264 export tests
verify both lyric and sequence fade windows. The guided compiler now accepts
sequence fade-in, static/none, handwriting, and ink-reveal blocks, placing all
sequence text after other lanes as production does. Other sequence effects
remain rejected pending composite-stream parity. Unrelated effects continue
to ignore fade_in_ms/fade_out_ms, matching the dispatcher.

See `docs/reviews/kri-29/coverage.md` for the concrete catalog and combination
ledger. This is implementation coverage, not rollout or device verification.
