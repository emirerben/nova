# KRI-1185: overlapped on-device export (Talking Save speed)

Evidence record for the PR that overlaps frames in the on-device export. Device: iPhone 13 Pro (iPhone14,2, 6 GB), USB,
Debug build of the app-hosted `TalkSaveBenchTests` (`TALKSAVE_RUN=1`). Scene: a 30 s phone-shaped take (1080x1920 HEVC
through a rotated 1920x1080 stream), Talking edit with a speaker image background, captions and the Kria branding; 948
frames. Raw lines are the `TALKSAVE` output of the benchmark.

## What was measured, and on which build

The device runs below are from the engine as of the first commit of this PR before the off switch, the issue/enqueue lock and
the matte precondition were added. Those three changes do not touch the drawing path. The final head was NOT re-measured on
the phone: the paired iPhone was offline (devicectl `tunnelState: unavailable`, no USB device on the Mac) for the whole
session after the last code change. Re-run before relying on the final-head numbers:

```
TEST_RUNNER_TALKSAVE_RUN=1 TEST_RUNNER_TALKSAVE_ARMS=base,p2,p3 TEST_RUNNER_TALKSAVE_RUNS=5 \
  xcodebuild test -project Kria.xcodeproj -scheme Kria -destination 'platform=iOS,id=<udid>' \
  -only-testing:KriaTests/TalkSaveBenchTests/testTalkSaveBench
```

`base` is depth 1 (what `-kria.exportFramesInFlight 1` selects: the old serial path), `p2` is the new default, `p3` and `p4`
are deeper depths.

### Five interleaved rounds, depth 1 against depth 3 (wall seconds, 948 frames)

| round | depth 1 | depth 3 |
| --- | --- | --- |
| 0 | 34.22 | 17.50 |
| 1 | 34.25 | 17.45 |
| 2 | 32.36 | 16.86 |
| 3 | 26.52 | 14.35 |
| 4 | 32.13 | 16.83 |
| median | 32.36 | 16.86 |

Median ratio 1.92x. Thermal state moved from nominal to serious across the five rounds; both arms ran in each round.

### Depth 1, 2, 3 and 4 interleaved (3 rounds, earlier run on the same clip)

| depth | rounds | median |
| --- | --- | --- |
| 1 | 34.42, 26.71, 33.16 | 33.16 |
| 2 | 16.94, 17.47, 17.32 | 17.32 |
| 3 | 17.06, 17.13, 17.55 | 17.13 |
| 4 | 16.43, 17.46, 17.24 | 17.24 |

Depth 2, 3 and 4 are the same speed: AVFoundation keeps exactly two requests outstanding on the phone, so a deeper pipeline
only costs memory. The default is 2.

### Composed frames

`TALKSAVE composed arm=p3 vs first base run: 0 of 948 composed frames differ` (SHA-256 of every frame as the compositor hands
it to the encoder, `RecipeWriter.frameTap`). A shorter 288-frame run gave 0 of 288 the same way.

### The encoded file is not byte-reproducible

The decoded pixels of two saves differ, and the cause is the hardware H.264 encoder, not Vision and not this change's frame
order. On the phone two depth-1 renders of the same recipe differed by up to 57 levels in about 43% of the sampled
pixels (mean 0.64), as did depth 3 against depth 1. On the Mac every depth-1 export decoded byte-identical; output diverged
only for a mask-cache hit at depth 3, the fastest pipeline (about 0.85 s against 2.4 s for 150 frames), by up to 45 levels
in 41% of pixels, different every run, with unchanged quality (mean error against the composed frames 5.47 at depth 3 and
5.47 at depth 1). Any exact file or decoded-pixel test must compare composed frames.

## Not measured

- A 4 GB phone (iPhone 12, SE 3). The 13 Pro has 6 GB.
- A real signed-in Save end to end. Upload, the server check and the 2/4/8/16/30 s poll sit outside the export and are untimed.
- Audio loudness comparison between depth 1 and depth 2.
- Peak memory on the phone, depth 1 against 2. The benchmark now samples `phys_footprint` and `os_proc_available_memory` every
  20 ms during each export (`TALKSAVE memory` lines) but could not run: the phone was offline. The memory argument for depth 2
  over 3 is by construction (each extra frame in flight holds an output buffer of about 8.3 MB, Vision's input of about 2 MB,
  its mask and Core Image scratch), not measured.
