# Native color-look progress

Rollout remains disabled. `goldenHourLook` is a distinct capability and is not
advertised by the default renderer. The guided compiler accepts golden hour only
for unrotated, exact-canvas footage. Native composition additionally requires
limited-range 8-bit H.264 with absent or Rec.709 transfer/primaries, and rejects
images, transformed clips and non-video tracks. Resize, HEVC and HDR remain open.

## Golden hour

`GoldenHourGrade` implements the fixed cloud grade on 8-bit limited-range YUV420
planes and NV12 pixel buffers. The luma LUT preserves the float parameter boundary
and byte truncation. Chroma correction uses the graded top-left luma of each
2x2 cell. Two 64-KiB tables avoid per-frame chroma floating-point work. The buffer
adapter preserves color attachments, respects padded strides, and leaves source
buffers unchanged for later requests.

The production convolution string looks like a fractional sharpening kernel, but
FFmpeg 7.1.5 parses coefficients as integers. It therefore becomes the identity
kernel. Native output preserves this existing behavior; changing the cloud grade
would be a separate product change. Implementation behavior was checked against
the versioned FFmpeg sources for
[EQ](https://github.com/FFmpeg/FFmpeg/blob/n7.1.5/libavfilter/vf_eq.c),
[color correction](https://github.com/FFmpeg/FFmpeg/blob/n7.1.5/libavfilter/vf_colorcorrect.c),
and [convolution](https://github.com/FFmpeg/FFmpeg/blob/n7.1.5/libavfilter/vf_convolution.c).

The production Docker image generated a 512x512 synthetic frame covering every
input luma/chroma pair, with different neighboring luma to catch incorrect chroma
sampling. Both the planar implementation and the pixel-buffer adapter match all
393,216 output bytes exactly. Three primitive tests pass, including padded rows,
attachment propagation, immutable input and invalid-plane rejection.

Reproduce from the repository root:

```bash
docker run --rm -i --user 0 --network none --entrypoint sh nova-render-api:local \
  < src/apps/api/scripts/generate_phone_look_fixtures.sh > /tmp/look-fixtures.tar
mkdir -p /tmp/look-fixtures
tar -xf /tmp/look-fixtures.tar -C /tmp/look-fixtures
cd src/apps/ios/Packages/KriaMediaEngine
KRIA_LOOK_FIXTURE_DIR=/tmp/look-fixtures swift test --filter GoldenHourGradeTests
```

## Moving footage and compositor

The shared preview/export compositor selects an NV12-only decoder for recipes
with looks, applies the grade before transitions and text, and leaves decoder
negotiation unchanged for recipes without looks. Existing recipes retain their
serialized shape and digest when no look is present.

A 160x96 moving H.264 fixture passes 12 preview/export samples including backward
seeks. The initial preview limit of 5 failed at 1.2 and 1.8 seconds. A separate
passthrough test showed the same failure without a new effect: Core Image's NV12
conversion differs from AVAssetImageGenerator's RGB output by up to 5.1684 channel
levels at saturated edges. The grade adds at most 0.1932 levels to that baseline.
Tests independently bound passthrough error below 5.5, absolute rendered error
below 6, and error beyond baseline below 0.5. Measured preview maximum is 5.3616;
export maximum is 5.0673. This is bounded visual parity, not RGB byte identity.
See [recorded samples](moving-look-report.json). Midpoint cloud/preview PNGs were
visually inspected. All 91 native tests, including the previous moving-transition
checks, pass; 37 focused backend recipe/compiler tests pass.

The signed Debug pilot adds `look-golden-hour` (32 cases total). It first prepares
an exact-canvas H.264 source from the bundled demo locally, recording preparation
separately, then exercises six-second look preview/export and backward seeking.
The signed build succeeds. Installation/run is pending the user's phone unlock.

Still required: resize-before-grade parity, full-size moving/graphics/transition
combinations, more decoder formats/ranges, physical-device throughput/memory, and
all other look presets. The buffer adapter intentionally rejects RGB, full-range
and higher-bit-depth inputs. Its allocation per frame needs device measurement.
