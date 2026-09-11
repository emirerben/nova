# Native color-look progress

Rollout remains disabled. The guided compiler still rejects every non-`none`
look. This is a verified primitive, not a completed preview/export creator flow.

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
393,216 output bytes exactly. Three native tests pass, including padded rows,
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

Still required: recipe/compiler capability, correct resize-before-grade ordering,
preview/export comparison with moving footage and graphics, decoder format/range
handling, physical-device throughput/memory, and all other look presets. The
buffer adapter intentionally rejects RGB, full-range and higher-bit-depth inputs.
Its current allocation per call also needs device measurement before integration.
