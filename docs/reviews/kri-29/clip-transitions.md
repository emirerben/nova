# Clip transition parity

Rollout remains disabled. The guided compiler still rejects transitions.

All five low-level transition types now have native render paths: `crossfade`,
`fade_black`, `fade_white`, `wipe_left`, and `wipe_right`. The four extended
types infer a separate `clipTransitions` capability in Python and Swift. It is
not part of the default advertised renderer set. The signed debug iPhone build
adds five six-second transition cases, with a 0.3-second overlap centered at 3s.
Installation and the physical pilot are pending the phone being unlocked.

The existing native crossfade blended in linear light. A real AVFoundation
preview of a black-to-white midpoint was visibly brighter than FFmpeg's encoded
channel blend. The compositor now converts only the clip blend into encoded
sRGB and back, retaining the existing working space for text.

`ClipTransitionCompositionTests` checks preview, H.264 export, and backward
seeking at the start, 20%, 50%, 80%, and end of the transition. Encoded grayscale
channels allow two levels of rounding in preview and three after H.264. The
complete native suite passes 86 tests. The focused backend recipe, device
request, guided compiler and dispatch suite passes 48 tests.
This verifies grayscale interpolation, not complete color-managed video parity.
Colored moving footage, source transfer functions, and iPhone measurements
remain required before the guided compiler can emit these transitions.

`phone_clip_transitions.json` contains 40 decoded FFmpeg reference frames for
gray-to-white fades and wipes. The reference was regenerated in the existing
`nova-render-api:local` production-parity image (FFmpeg 7.1.5); its pixels match
local FFmpeg 8.1.1 exactly. Reproduce it with
`src/apps/api/scripts/generate_phone_transition_fixture.py`. Native tests check
every pixel across the middle row, in reversed time order, in both preview and
H.264 export (three/five channel levels of tolerance respectively). Pure timing
tests independently check the Y-plane curve and exact wipe boundaries.

These fixtures cover grayscale only. The fade background currently uses
limited-range BT.601 conversion; footage with other transfer/matrix tags,
colored moving patterns and overlapping source audio remain unverified.

Reference: FFmpeg `xfade=transition=fade:duration=1:offset=1` using two-second
black and white 16×16 lavfi sources. At 1.5 seconds the RGB output is 127.
When inspecting AVFoundation output, compare encoded bytes consistently:
converting its Rec.709-tagged image into sRGB changes the numerical midpoint.

Further transition work must preserve FFmpeg's actual curves and boundaries.
Its fadeblack/fadewhite use nested reversed-weight mixes and smoothstep with
a 0.2 phase; the black/white plane values include full-range extrema even for
limited-range YUV. Wipes use integer pixel boundaries and strict `x > z`
comparisons, so they differ by a pixel at matching progress. Source:
[FFmpeg vf_xfade.c](https://github.com/FFmpeg/FFmpeg/blob/n8.0/libavfilter/vf_xfade.c).
