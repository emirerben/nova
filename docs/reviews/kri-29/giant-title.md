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
