# Native giant-title increment — 2026-09-11

Rollout remains disabled. This increment supports giant-title-wipe with static,
none, fade-in, scale-up, slide-up/down/in, pop-in, bounce, ink-reveal and lyric-line.
Other painter combinations still reject the contract.

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
