# Preserve full-range white in native H.264 exports

Related incidents: KRI-1173 (speaker color fills), KRI-1231 (split-screen acceptance).
Claim: bounded native exporter correction. No iPhone deployment or overall split-screen completion claim.

## Request and acceptance

A creator's chosen white background must remain white in the exported file while the speaker stays visible. The existing `SpeakerBackgroundCompositionTests.testColourFillShowsTheRequestedColourBehindTheSpeaker` requires white and green within 2/255, and preview/export agreement within 3/255. Its expectations are unchanged.

Independent expectations are the existing color-fill acceptance test and standard ITU-R BT.709 video-range endpoints/matrix. This change is about recipe/export fidelity, not interpretation of requested source moments.

## Measured root cause

The real failing recipe's preview and last pre-append BGRA samples were 255/255/255. Its H.264 file decoded to Y=234, Cb=Cr=128 and final sRGB 253/253/252. Software FFmpeg decoding independently confirmed the encoded YUV deficit.

A standalone `VTPixelTransferSessionTransferImage` with no encoder reproduces Y=234 from the retained exact-white BGRA. Explicit BT.709 destination properties do not alter it. This localizes the observable deficit before H.264 quantization, at the implicit BGRA-to-YUV transfer.

The replacement uses Accelerate/vImage's standard BT.709 matrix, full-range BGRA, video-range Y 16–235 and CbCr 16–240, and centered 2×2 chroma. It maps white to Y=235; the encoded file preserves it and the same final sRGB path yields 254/254/254, within the unchanged tolerance. All six original white sample locations improve, and the speaker pixel is unchanged. Twelve-color baseline/candidate measurements are retained in `boundary-observations.json`.

This is a general, faithfully rounded color conversion, not a white-specific adjustment, a bitrate change, or an assertion relaxation. H.264 High, video-range 4:2:0, BT.709, canvas dimensions and audio handling remain unchanged. Center chroma metadata describes the actual new subsampling.

## Regression coverage

Implementation commit `5a68950a6162ebc0c5268c9a0e2c95168d7be92d` passed 75 tests with 0 failures and 0 skips on 2026-11-07, 00:10:58–00:11:23 UTC. Actual tests took 20.366 seconds.

```sh
swift test --package-path src/apps/ios/Packages/KriaMediaEngine --jobs 2 \
  --filter 'RecipeEncodingPixelsTests|SpeakerBackgroundCompositionTests|SpeakerBackgroundTests|DrawingExportTests|FreezeFrameExportTests|OffsetVideoTrackExportTests'
```

- Six new converter tests: twelve independent color vectors; padded row strides; retained buffers; PTS, duration and valid/invalid DTS; mixed centered 2×2 chroma; colored odd edges; invalid formats/dimensions; transfer/primaries and aperture/aspect metadata.
- Unchanged speaker-background export acceptance, including the original white failure.
- Neighboring drawing, freeze-frame and offset-video exports.
- No source-moment selection, prompt, provider or backend behavior changed.

Failure modes were recorded before implementation. The existing unchanged export test and baseline encoded probe are the red controls. The prior baseline result was 80/81 with white-fill as the sole failure. The current branch does not contain all split-screen feature tests, so the 75-case result is not a claim that its original 81-case selection has been rerun after integration.

## Review, scope and remaining gates

Independent source review found no concrete production blocker. Its requested mixed/odd-edge, metadata, valid-DTS and invalid-input tests are included and passing. A failed scratch XCTest loader attempt is not counted as execution evidence; the actual Swift package batch subsequently ran and passed.

Required preship and exact-head CI remain publication gates. The combined split-screen head must rerun its affected engine selection after integration. Physical iPhone performance, complete native aggregate, odd-size H.264 output compatibility and rollout remain unverified. Odd dimensions are tested at the conversion boundary, not represented as a new H.264 support promise. The existing compositor's broader sRGB/Rec.709 interpretation is unchanged and is not claimed fixed by this patch.

Media and diagnostic logs remain private local artifacts. No raw footage, account data, tokens or signed URLs are committed.
