# Native giant-title increment — 2026-09-11

Rollout remains disabled. Giant-title-wipe now supports the shared text painters,
including typewriter, stream-in, karaoke, smooth-type, staggered-slice and
handwriting. Dissolve preserves production's static opening source: the cloud
sequence never applies the later giant-title camera transform to that effect.

Current validation: 396 cloud RGBA frames, 85 timing samples, 82 native tests and
87 backend checks pass. The maximum RGBA error remains 3.359/255 under the 3.5/255
limit, with independent foreground-edge checks. Preview and H.264 composition
tests include handwriting, nonzero start/end times and backward seeking. A
26-case physical-device catalog is being exercised; its result is recorded below.
The following sections retain the evidence from each increment.

## Initial vector increment

The compiler resolves the production target-glyph camera origin. Native rendering
retains font outlines and redraws at the final camera scale, preserving sharp
edges through the 60× zoom. The hold, cubic-bezier zoom and byte-quantized fade
match 85 samples from the actual cloud helper. Entrance opacity applies to each
paint; the camera fade applies to the completed group.

The actual cloud renderer supplies 192 RGBA reference frames covering the eleven
effects, rotation, anchors, target glyphs, shaped text, outline, shadow and glow.
All pass: maximum mean RGBA error 3.722/255 (1.46%). Styled frames allow 4/255,
others 3.5/255. Independent foreground-edge checks bound displacement to two
pixels, or three for the rotated style. The worst colored frame has white glyph
edges within one pixel; remaining error includes antialiasing and stroke edges.
This is raster tolerance, not byte-identical output.

## Blur and color parity

Skia's device-space sigma is capped at 128; its source-mask clip outset is also
capped at 128, independently of the blur's wider support. The second cap matters
when the camera moves into an oversized glyph counter. Omitting it roughly
doubled the visible glow in the failing frame. Native rendering now uses the
same mask boundary and Skia's three-box widths. CIBoxBlur inputRadius was measured
as full kernel width; using half the width makes the tails too narrow.

Core Image tint matrices operate on straight colors. RGB bias plus a single alpha
multiplier preserves coverage; multiplying RGB by alpha as well darkens the glow.
Painter contexts now use sRGB working color. A one-pixel premultiplied-color test
locks this behavior. Glow entrance alpha is squared where the cloud applies it
twice; ordinary shadows and handwriting retain their respective alpha semantics.

Primary implementation references:
[SkDraw.cpp](https://github.com/google/skia/blob/main/src/core/SkDraw.cpp),
[SkMaskBlurFilter.cpp](https://github.com/google/skia/blob/main/src/core/SkMaskBlurFilter.cpp),
[SkBlurMaskFilterImpl.cpp](https://github.com/google/skia/blob/main/src/core/SkBlurMaskFilterImpl.cpp).

## Validation and physical pilot

The full native package suite passed 80 tests, then the added invalid-contract
test passed separately. Preview and H.264 export both verify the layer's nonzero
start time, zoom growth, end time and backward seeking. Backend text/compiler and
reference fixtures passed 84 tests. Mobile OpenAPI regeneration/check and the
signed physical-device app build passed.

The catalog now contains 20 cases. Both new cases passed compositor frame requests
and produced six-second 1080×1920 H.264 files on the iPhone 13 Pro. Plain giant-title
export took 4.51 seconds; colored glow took 8.38 seconds, then 8.22 seconds on repeat.
Frames at 1, 3.9, 4.15, 4.35, 4.7 and 5.3 seconds were visually inspected in both
actual phone exports. The footage already contains its own Corfu label.

Both full pilot runs completed 19/20 exports. The first static case reported
AVFoundation -11847 Operation Interrupted; on repeat static passed and plain
giant-title reported the same interruption (underlying OSStatus -16121). No cause
is established. The glowing title succeeded in both runs. These are successful
individual exports, not a stable full-catalog or interruption/recovery gate.
Reports: [first](iphone13pro-giant-first.json), [repeat](iphone13pro-giant-repeat.json).

The updated interactive picker is left on the phone. Displayed playback FPS,
seek-to-display p95, 60-second exports, memory/thermal measurements and recovery
remain outstanding alongside the broader creator matrix.


## Discrete reveal follow-through

Typewriter and stream-in now retain a vector run set for the current text/cursor
sample. It is replaced only when that sample changes. The giant camera redraws
those glyphs at output resolution; it never enlarges a prefix bitmap. A delayed
schedule changes letters during the zoom, and a rotated, shaped stream case
checks cursor and small-font drawing.

At smaller sizes, font hinting preserves the cloud's stroke weight. Explicit
fractional positioning with Core Text subpixel quantization disabled fixes a
separate half-pixel phase difference. Above a 256-pixel device font size, retained
outlines bound glyph raster-cache use during the zoom. This reduces the full
240-frame set's maximum mean RGBA error to 2.385/255. Every frame now uses the
stricter 3.5/255 limit, including styles; the independent edge checks remain.

All 81 native tests pass, including delayed-typewriter preview/export and backward
seeking. The focused backend/compiler/reveal set passes 61 tests. This later
increment has not yet been installed on the phone; the physical pilot above
records the preceding plain/glow implementation.


## Karaoke and smooth reveal follow-through

Timed karaoke colors and per-line smooth-reveal masks now draw through the giant
camera. Smooth motion blur is baked in sRGB before composition; rendering its
result through sRGB and linear consumer contexts agrees within one byte. The
planner reserves padded blur intermediates using a conservative bound on the
product of decreasing entrance blur and increasing camera scale.

Dense colored glows exposed another parity detail: Skia blurs and blends glyphs
individually, with integer premultiplied-color rounding. The native painter now
clips each glyph source to its needed bounds, uses GPU box blurs and a Metal
color kernel for that byte rounding, and composites in the same glyph order.
A whole-line glow retained color in tiny tails that the individual Skia masks
rounded away. Gradient clips retain small-size font hinting.

The current reference set has 324 frames, including gradient-only, glow/shadow,
combined motion blur, rotated small text, highlights changing during zoom, and a
stream-in speed of 0.25 so its prefix remains active late in the layer. Maximum
mean RGBA error is 3.359/255, within the unchanged 3.5/255 limit. All 82 native
tests and 72 focused backend tests pass. Preview/H.264 export/backward-seek checks
include karaoke and smooth reveal. This increment is not yet phone-tested.


## Staggered-slice follow-through

Partial staggered glyphs now keep independent rotations, translations and
byte-quantized group opacity inside the giant camera. The partial stage omits the
overlay rotation exactly as the cloud does; the settled branch restores it.
One bounded transparency intermediate is reserved. Font preparation is reused
for runs sharing an asset and size, including per-glyph runs.

All 348 reference frames, 82 native tests and 63 focused backend tests pass. The
maximum RGBA error remains 3.359/255. The export/seek test includes the slower
staggered case. This increment has not yet been installed on the phone.


## Handwriting and dissolve increment

Handwriting transforms its centerline paths at the final camera scale, with
round caps and joins, then paints glow, black shadow, outline and foreground in
cloud order. Both handwriting glow and font glow use entrance alpha squared.
The same bounded device-space blur helper serves glyphs and pen paths.

Individual masks matched while the aggregate pen glow was initially too bright.
Skia source-over uses `src + floor(dst * (256 - srcAlpha) / 256)` on premultiplied
bytes; Core Graphics rounds the destination product instead. Across 99 faint
paths, that difference reached 25/255 mean RGBA error. The handwriting shadow
compositor now preserves the integer operation per path using packed byte lanes.
The rotated glowing handwriting case dropped to 0.312/255 maximum mean error.
Reference: [SkColorPriv.h](https://github.com/google/skia/blob/main/src/core/SkColorPriv.h).

New fixtures cover handwriting, rotated glow/outline/shadow, fading purple glow,
and a rotated gradient. The compiler fixtures also validate each serialized
layer through the public model, rather than relying on unchecked model copies.
A production `_generate_overlay_sequence` spy compares every dissolve source and
sample with/without the theme and confirms the overlay-index seed is retained.
The native wire model still rejects a dissolve carrying a giant camera transform.


### Expanded device pilot: incomplete

The first 26-case run recorded nine successful exports and then stopped updating.
A retry of ordinary handwriting plus the six new giant combinations completed
handwriting (2.66s), giant typewriter (7.18s), smooth reveal (9.39s) and staggered
slice (11.51s), then stopped updating during giant karaoke export. All four
successful cases also passed six compositor frame requests, including a backward
jump. Reports: [26-case partial](iphone13pro-26-partial.json),
[selected partial](iphone13pro-giant-selected-partial.json).

The test harness now saves the active case, preparation/frame/export phase,
preparation time, aggregate frame-request time, export progress and application
lifecycle events. A local debug-only export trace distinguishes recipe preparation,
writer initialization and completion. `-device-effects-only` accepts a comma-separated
case list to reproduce an individual failure without rerunning the entire catalog.

The next launch was explicitly refused because the iPhone was locked. The earlier
stalls are not attributed to that lock without lifecycle evidence. Karaoke, giant
handwriting glow and themed dissolve still need their physical export checks;
the expanded catalog is not a passing release gate.


## Giant-title handwriting performance — increment 1 (profiler + L0/L1 caching)

[`iphone32-optimized.json`](iphone32-optimized.json) measured giant-title
handwriting at **106.128 s to export 6 s** on the user's iPhone 13 Pro, even with
`SWIFT_OPTIMIZATION_LEVEL=-O` — golden-hour exported the same duration in 3.078 s.
`phone_rollout.py` hard-rejects the combination for this reason. The earlier
32-case pilot report asked to "profile its per-stroke shadow blur and compositing
path before enabling it," without establishing the cause.

**Root cause.** `NativeGiantHandwritingPainter.image` called the (then-stateless)
static `NativeSkiaShadowPainter.draw` once per stroke per blur layer per frame —
up to 297 calls/frame for this fixture's 99 strokes × 3 blur layers, though the
giant-title painter's own settled fast-path skips most of the zoom phase, leaving
roughly 18,700 calls across the whole export. Each call allocated a fresh mask
`CGContext`, ran the CIBoxBlur/tint pipeline, and finished with a **synchronous
`imageContext.createCGImage` GPU readback** — a per-call cost estimated (not yet
measured on-device) to dominate the observed 106 s.

**Fix (byte-exact, no Metal).** `NativeGiantHandwritingPainter` became a stateful
class retained by `NativeGiantTitlePainter` across frames, mirroring the
non-giant `NativeHandwritingPainter`'s settled-shadow cache:

- A stroke's centerline path and stroked ink bounds are cached once the stroke is
  fully revealed (`endProgress <= progress`) — `visiblePoints(at:)` returns the
  identical point list for any later progress, so this is exact by construction.
  Ink bounds were also being recomputed once PER BLUR LAYER (3x/stroke); now once.
- A settled stroke's blurred, tinted shadow tile
  (`NativeSkiaShadowPainter.tile`, split out of `draw` so a caller can retain the
  returned `CGImage` instead of only compositing it once) is cached per
  `(blurLayerIndex, strokeIndex)`, keyed by `(transform, opacity)` — the only two
  inputs it depends on beyond the stroke itself. Any change to that signature
  invalidates every cached tile before the next draw, so a stale byte can never be
  served — only real GPU/CPU work can be skipped.
- The tile cache carries its own 32 MiB soft budget, independent of
  `maxBitmapBytes`; exceeding it (comfortably above the ~15 MB this fixture needs)
  only disables caching for the overflow tile, never a correctness risk.

For the `handwriting` effect specifically, `TextTransformTiming` holds
`alpha == 1, scale == 1, translate == 0, blur == 0` for the whole writing phase,
so the composed camera transform is `.identity` until the zoom starts (~68% of
the duration) — the same frame-invariant-transform precondition the non-giant
painter's cache already relies on. This collapses the writing phase (the
majority of drawn frames) from one real render per stroke per frame to one real
render per stroke total.

**A real bug this design caught before it shipped.** The very first call into
the new stateful painter is `settledImage()`'s one-time warm-up
(`progress: 1, transform: .identity`), called before any real animated frame —
not a monotonically-forward playback step. Initially the path/ink-bounds cache
trusted a cache hit as soon as *any* prior call had marked a stroke settled,
without re-checking that the *current* call's progress also qualified. That let
the progress-1 warm-up poison every subsequent lower-progress frame with the
fully-written path. Caught by the cloud-reference suite (`GiantTitleTests`)
immediately — 10 failures, up to 14.48/255 mean error against a 3.5/255 limit —
before any device build. Fixed by gating the cache read on
`progress >= stroke.endProgress` for the *current* call, not just cache presence.

**Verification.**
- `swift test` (`src/apps/ios/Packages/KriaMediaEngine`): 132/132 tests pass,
  including the full 396-frame cloud-reference suite in `GiantTitleTests`
  (unmodified, unchanged tolerances) and `HandwritingTests`. Per-handwriting-row
  max RGBA error confirmed byte-identical to the pre-change baseline (compared via
  `git stash`): `handwriting-legacy` 0.168, `handwriting-glow` 0.309,
  `handwriting-fading-glow` 0.408, `handwriting-gradient` 0.107 — all far under
  the 3.5/255 budget, all unchanged by this PR.
- New `GiantTitleShadowParityTests`: zero-tolerance `XCTAssertEqual` on raw RGBA8
  bytes between the cached painter (driven through the real warm-up-then-forward-
  then-backward sequence) and a freshly-constructed, never-reused reference
  painter, across all four handwriting fixture rows. A second test forces the
  32 MiB cache budget down to 1 byte and re-checks the same parity, pinning the
  starved-cache fallback path.
- New `GiantTitleProfileTests`: counter-based (not wall-clock, to avoid CI
  flakiness) proof the cache actually engages — a repeated identical-signature
  call produces zero fresh `shadow.tile` builds and all cache hits; a
  transform change invalidates every cached tile.
- `make ios-verify` (`KRIA_IOS_TEST_MODE=unit`): 285/285 `KriaTests` pass. App
  target (including `DeviceEffectsView`'s new profiler wiring) builds clean for
  simulator.
- Backend: unaffected by this increment (no `src/apps/api` changes); ran
  `tests/kria/`, `tests/services/test_phone_rollout.py`, `tests/test_device_render.py`,
  `tests/routes/test_device_render.py` (308 passed) and `kria_contracts --check`
  anyway, since the shared render-recipe contract is a blast-radius neighbor.

**Instrumentation added, not yet run on-device.** `RenderProfiler` (signpost +
accumulator, ~1 ns overhead when disabled) buckets each stage of
`NativeSkiaShadowPainter.tile` (mask alloc, mask stroke, CI blur graph, the
`createCGImage` readback, the composite) plus the giant-handwriting painter's own
path/ink-bounds/frame stages. `DeviceEffectsView` surfaces a `profile` snapshot in
its report JSON when launched with `-render-profile`, alongside the existing
`export_seconds`.

**Outstanding — the actual gate.** This increment has NOT been run on the user's
iPhone 13 Pro yet. Until it is:
- The stage-by-stage `profile` breakdown that would confirm or correct the
  synchronous-readback hypothesis is unmeasured.
- The before/after `export_seconds` for `giant-title-handwriting` is unmeasured;
  expected to drop from 106.128 s given the writing phase now does ~1 real render
  per stroke instead of ~1 per stroke per frame, but this is a prediction, not a
  result.
- Whether this alone reaches the release gate (60 s export in <=120 s, i.e. a 6 s
  case at <=~9 s) is unknown. If it doesn't, GPU-batched compositing (PR 2 in
  `plans/kri-29-phone-rendering.md`'s handwriting-perf plan) is the next rung.
- `phone_rollout.py`'s rejection of giant-title handwriting stays in place
  regardless of this PR's outcome — the pilot's `PHONE_RENDER_VERIFIED_FEATURES`
  flag is deliberately not touched here (see that plan's deploy-ordering
  section); lifting it needs the on-device number confirmed first.

Run `-device-effects-only giant-title-handwriting -render-profile` on the
iPhone 13 Pro (see `docs/runbooks/ios-development.md` for the physical-build
steps) and record the result here as the next entry in this file.
