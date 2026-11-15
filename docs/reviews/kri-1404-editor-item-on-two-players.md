# KRI-1404: the editor aborts when the live preview item is put on a second player

## Incident

TestFlight build 1977 (iPhone15,4 = iPhone 15, iOS 26.5.2) aborted twice on 2026-11-13:

- 19:23:33 UTC, about 35 s after opening the editor on the finished "ylab" Talking video (thread
  `8044373b`, job `1f2de191`: 91 s, five takes, 45 speech-cleanup cuts, 42 caption cues);
- 20:20:12 UTC, the same build in a later session.

Both crash reports have the same shape:

- `EXC_CRASH / SIGABRT`, `abort() called` on the main thread;
- last exception backtrace `objc_exception_throw` ← `-[AVPlayerItem _attachToPlayer:]` ←
  `-[AVPlayer initWithPlayerItem:]` ← Kria, from a resumed main-actor task;
- at the same moment another thread is inside `remoteXPCPlayer_RemoveFromPlayQueue`, so a different player
  is still releasing an item.

## Root cause

While the finished video plays, `NativeEditorSession` warms the rebuilt live preview on its own player
(`prerolledPlayer(for:at:)` → `AVPlayer(playerItem:)`). The warm player was then dropped with
`replaceCurrentItem(with: nil)` when it was not adopted. That happens when:

1. the creator paused or scrubbed while it warmed, or it drifted more than 0.35 s;
2. the warm-up missed its ~2 s budget or its seek/preroll failed (likely for a long, many-cut edit on a
   phone);
3. a retained preview drifted and the corrective seek missed.

The plain swap then called `AVPlayer(playerItem:)` on the **same** item. AVFoundation releases an item from
its player asynchronously, so the item was still attached. That throws "An AVPlayerItem cannot be associated
with more than one instance of AVPlayer", and the uncaught exception aborts the app. The warm hand-off came
in with #2084 (2026-11-03) and #2221 (2026-11-08).

## Fix

Once the warm-up has built its player on the item, that player is the only one that may hold the item.

- `prerolledPlayer` returns a `WarmedLivePlayer`: the player, plus whether it is prerolled. It returns that
  player on a miss as well, instead of detaching it.
- The rebuild installs the prerolled player for the smooth hand-off. Otherwise it runs the plain swap (seek
  or position) on that same player.
- A stale build (the document changed meanwhile) still detaches it and stops.

## Evidence (simulator, iPhone Air, iOS 26.5, arm64 Debug)

`KriaTests/NativeEditorPreviewWorkSessionTests/testPausingWhileTheLivePreviewWarmsOverTheFinishedVideoDoesNotAttachItsItemTwice`
drives case 1 through the production warm-up:

1. the finished video is playing;
2. the creator pauses inside the warm-up;
3. the live preview is installed.

The test asserts that the provider ran once, the preview is ready and paused, the finished player is
detached, and Play starts the live preview.

- **Red control** (base `dcdd1ef45`, test only): the test process crashed with signal `abrt`. The session log
  of the result bundle contains "An AVPlayerItem cannot be associated with more than one instance of
  AVPlayer", the same exception as on the phone. The other six tests in the class passed.
- **Green** (with the fix): the test passes. `testRetainedHandoffThatMissesItsPrerollFallsBackToThePlainSwapAndStaysPlayable`
  now models a real miss: its provider attaches the item and returns it not prerolled. It covers cases 2 and 3
  on the retained path and asserts that the plain swap runs on that same player.
- **Whole unit suite** with the fix: `-only-testing:KriaTests`, 3,270 tests passed, 0 failed, no restarts.

## Not covered

- No physical-device run: owed on the first TestFlight build with this change, using the ylab video.
- The Kria frames in the phone reports are not symbolicated, because the build-1977 dSYM is not on this Mac.
  The match rests on the AVFoundation frames, the concurrent `RemoveFromPlayQueue` in both reports, and the
  same exception reproduced by the test.
