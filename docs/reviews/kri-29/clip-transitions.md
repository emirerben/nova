# Clip transition parity

Rollout remains disabled. The guided compiler still rejects transitions.

The existing native crossfade blended in linear light. A real AVFoundation
preview of a black-to-white midpoint was visibly brighter than FFmpeg's encoded
channel blend. The compositor now converts only the clip blend into encoded
sRGB and back, retaining the existing working space for text.

`ClipTransitionCompositionTests` checks preview, H.264 export, and backward
seeking at the start, 20%, 50%, 80%, and end of the transition. Encoded grayscale
channels allow two levels of rounding in preview and three after H.264. The
existing 82 native tests passed; the added transition test passes separately.
This verifies grayscale interpolation, not complete color-managed video parity.
Colored moving footage, source transfer functions, and iPhone measurements
remain required before the guided compiler can emit these transitions.

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
