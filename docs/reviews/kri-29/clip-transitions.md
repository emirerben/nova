# Clip transition parity

Rollout remains disabled. The guided compiler now represents crossfade, black-dip,
and white-flash boundaries when their exact source and overlap timing is supported.
Source-audio transitions and sub-millisecond boundary programs still fail closed.

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
complete native suite passes 87 tests, including the explicit moving-video run.
The focused backend recipe, device request, guided compiler and dispatch suite
passes 55 tests; an additional short-clip clamp test passes in the compiler suite.
This verifies grayscale interpolation, not complete color-managed video parity.
Further source transfer-function coverage and iPhone measurements remain
required before production rollout.

`phone_clip_transitions.json` contains 40 decoded FFmpeg reference frames for
gray-to-white fades and wipes. The reference was regenerated in the existing
`nova-render-api:local` production-parity image (FFmpeg 7.1.5); its pixels match
local FFmpeg 8.1.1 exactly. Reproduce it with
`src/apps/api/scripts/generate_phone_transition_fixture.py`. Native tests check
every pixel across the middle row, in reversed time order, in both preview and
H.264 export (three/five channel levels of tolerance respectively). Pure timing
tests independently check the Y-plane curve and exact wipe boundaries.

These small row fixtures cover grayscale. The separate moving-video comparison
below covers tagged SDR footage. The fade background uses limited-range BT.601
conversion; further matrix/transfer combinations and source audio remain open.

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

## Moving SDR video

`ColoredClipTransitionTests` compares five transitions at six times in both
preview and H.264 export, including before/after and a backward jump. Sources
are two synthetic moving-color clips, encoded and tagged Rec.709 inside the
production render image. The initial comparison caught source brightening even
outside the transition. Explicitly preserving decoded channel values at the
Core Image input corrected that extra transfer-curve conversion.

The 60 sampled frames pass separate average RGB error limits: 5 in preview,
7 after the additional native H.264/chroma-resampling pass. Observed maxima are
4.20 and 6.01, respectively; the export baseline outside a transition is already
about 5 on this small saturated pattern. These are bounded comparisons, not
byte-equivalent output. Midpoint cloud/preview/export PNGs were inspected.
Metrics: [moving-transition-report.json](moving-transition-report.json).

Reproduce using a Docker engine that can return stdout to the host (no shared
host mount is required):

```sh
mkdir -p /private/tmp/kri29-transition-video
docker run --rm -i --user 0 --network none --entrypoint sh nova-render-api:local < src/apps/api/scripts/generate_phone_transition_videos.sh > /private/tmp/kri29-transition-video.tar
tar -xf /private/tmp/kri29-transition-video.tar -C /private/tmp/kri29-transition-video
cd src/apps/ios/Packages/KriaMediaEngine
KRIA_TRANSITION_VIDEO_FIXTURE_DIR=/private/tmp/kri29-transition-video swift test --filter ColoredClipTransitionTests
```

Without the environment path this integration test explicitly skips; the small
committed reference fixtures always run. Generated videos stay outside git.
The signed iPhone build includes the SDR correction and 31 total debug cases;
physical testing remains pending the user's unlock response.
