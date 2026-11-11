# KRI-1180: talk-bench video background (debug-only fix)

Durable summary of the device runs behind this change. The raw run logs stay local.

## Problem

Scripted device bench runs with a video background (`video-cin0` and related scenarios) failed 18 of 18 with
"Record should enable". `TeleprompterRecordingFlow.load()` awaited `installBenchBackground()` before the
camera was prepared. The bench's synthetic 10 s video takes about 41.7 s to encode on an iPhone 13 Pro
(1.8 s on a Mac), so Record stayed disabled past the test's 20 s wait.

## Fix

The video background is made in a detached task and `load()` no longer waits for it. `makeVideo` checks the
writer status while waiting, cancels the writer after a 150 s watchdog, retries up to 3 attempts, and writes
every attempt to the `bench.video.make` fact. The product never encodes a background (it imports a picked
file), so no creator-visible behaviour changes.

## Measured, iPhone 13 Pro, main plus this branch, Debug

- 6 of 6 video runs passed: `video-cin0` x3 and `video-cin0-old` x3. Every run logged
  `bench.video.make: attempt1=ok in 41.7 s`.
- One `photo-cin0` regression run passed.
- Live preview ran at 30.0 fps with 0 drops. Shipped levers: per-frame consume p95 7.8 ms, camera-to-screen
  p95 38.7 ms. Levers off: 30.3 ms and 62.8 ms. The mask is one frame older with the levers on.

## Simulator

`TalkBenchVideoFixtureTests` passes: the file is playable, about 10 s, 720x1280, and `LoopingVideoBackground`
decodes a frame.

## Not exercised

The watchdog, writer-failed and retry paths were not exercised on the phone because the encode never failed.
