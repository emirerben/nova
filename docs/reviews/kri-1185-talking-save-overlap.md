# KRI-1185: overlapped on-device export (Talking Save speed)

Evidence record for the PR that overlaps frames in the on-device export. Device: iPhone 13 Pro (iPhone14,2, 6 GB), USB,
optimized Debug build of the app-hosted `TalkSaveBenchTests` (`TALKSAVE_RUN=1`). Scene: a 30 s phone-shaped take (1080x1920 HEVC
through a rotated 1920x1080 stream), Talking edit with a speaker image background, captions and the Kria branding; 948
frames. Raw lines are the `TALKSAVE` output of the benchmark.

## Final head (default depth 2, off switch in place)

Measured on the same phone and scene after the off switch, the issue lock and the matte precondition were added (engine as at
`a81f1a47c`, plus the benchmark warm-up change below). Built as the earlier runs were, `SWIFT_OPTIMIZATION_LEVEL=-O GCC_OPTIMIZATION_LEVEL=s` on the Debug
configuration; a plain unoptimized Debug build is about 2.5x slower in both arms (depth 1 median 86.5 s, depth 2 median 45.1 s,
still 1.9x) and is not comparable with these numbers. `base` is depth 1, the arm that
`-kria.exportFramesInFlight 1` selects (`RecipeWriter` returns the player's composition untouched); `p2` is the new default.

Interleaved, 5 rounds, wall seconds for 948 frames:

| round | depth 1 (old path) | depth 2 (default) |
| --- | --- | --- |
| 0 | 33.85 | 31.37 |
| 1 | 34.65 | 18.43 |
| 2 | 36.10 | 16.48 |
| 3 | 33.76 | 20.51 |
| 4 | 33.78 | 16.41 |
| median | 33.85 | 18.43 |

The medians give 1.84x. Round 0 of depth 2 (31.37 s) is an outlier that I did not drop: the thermal state went from fair to
serious during that export. Rounds 1 to 4 alone give 34.2 s against 17.5 s, 1.95x. Depth 1 on the final head (33.85 s median)
matches the old path measured before (32.36 s median over five rounds, 33.16 s over three).

Peak memory, the growth of the app's `phys_footprint` between the start of an export and its peak (sampled every 20 ms in the
same five rounds, so each depth-1 and depth-2 pair shares the phone's state):

| | depth 1 | depth 2 |
| --- | --- | --- |
| growth per export (MB), rounds 0 to 4 | 110.6, 128.1, 108.2, 122.3, 134.9 | 225.6, 228.6, 235.4, 206.2, 217.8 |
| median | 122.3 | 225.6 |

The default depth costs about 100 MB more at peak than depth 1 on this scene (a 30 s single take with captions and an image
background; no look, one clip). That is more than the buffer estimate (about 8.3 MB output, about 2 MB Vision input, its mask
and Core Image scratch per frame in flight), so the extra cost is in the Vision and Core Image working set; a third frame in
flight would likely add about the same again for no speed. The lowest `os_proc_available_memory` seen was 2.36 GB (depth 2,
last round) against 2.49 GB (depth 1), which includes about 50 MB per export of the benchmark's own retained decoded frames.
The 13 Pro has 6 GB; a 4 GB phone was not tested, and a 60 s multi-clip edit with a look was not built.

Composed frames, 2 rounds with the frame tap on (`TALKSAVE_TAP=1`): `TALKSAVE composed arm=p2 vs first base run: 0 of 948 composed
frames differ`. The tap hashes every frame on the encoder thread, so the wall times of that run (36.7 and 37.8 s against 29.8
and 28.9 s) are not a speed measurement. Decoded files in that run: depth 1 repeat identical, depth 2 against its own first
run up to 42 levels in about 39% of pixels, the encoder noise described below.

Reproduce:

```
TEST_RUNNER_TALKSAVE_RUN=1 TEST_RUNNER_TALKSAVE_ARMS=base,p2 TEST_RUNNER_TALKSAVE_RUNS=5 \
  xcodebuild test -project Kria.xcodeproj -scheme Kria -destination 'platform=iOS,id=<udid>' \
  -only-testing:KriaTests/TalkSaveBenchTests/testTalkSaveBench SWIFT_OPTIMIZATION_LEVEL=-O GCC_OPTIMIZATION_LEVEL=s
```

## Earlier head (before the off switch)

`p3` and `p4` are depths 3 and 4.

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
only costs memory. The default is 2 on a phone with 5 GB or more of memory and 1 below that (about 100 MB more at the peak, measured on a 6 GB phone; 4 GB phones untested).

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
- Peak memory for a 60 s multi-clip edit with a look, and on a 4 GB phone. Depth 3 against depth 2 memory was not measured
  either; the argument for 2 over 3 is the unchanged speed.
