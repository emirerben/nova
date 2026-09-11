# iPhone 13 Pro: 32-case functional pilot

2026-09-11. Implementation: `50e011ac9`. Physical device: the user's iPhone 13 Pro.
All 32 distinct cases completed six preview-frame requests (including backward
seeking) and six-second 1080x1920 exports across three runs. No case returned an
export error. This is functional evidence, not a passed rollout/performance gate.

## Runs and evidence

1. Standard Debug: first 24 cases exported. Giant-title handwriting continued
   advancing but became excessively slow near its zoom. The agent interrupted
   this run after recording its progress to exercise the remaining cases.
   [Report](iphone32-debug-first.json).
2. Standard Debug: giant-title dissolve and all five transitions exported.
   Golden hour advanced slowly; the agent interrupted this run to install the
   optimized build. [Report](iphone32-debug-tail.json).
3. Same Debug pilot compiled with `SWIFT_OPTIMIZATION_LEVEL=-O`: both remaining
   cases completed. Handwriting export **106.128 seconds**, golden hour export
   **3.078 seconds**, for six seconds of media each. Their six preview requests
   took 0.977 and 0.405 seconds respectively. [Report](iphone32-optimized.json).

The Xcode build log confirmed `KriaMediaEngine -O`; the DEBUG pilot UI was retained.
The first two reports intentionally retain unfinished phases. They must not be
described as clean 32/32 batch runs. The combined distinct-case result is 32/32.
The final report contains inactive/active events and ends in the background;
its lifecycle events are retained verbatim. This was not a controlled recovery test.

## Implication

Golden-hour functional preview/export now has physical-device evidence. All five
clip transitions and the additional giant-title cases also exported on this
device. Giant-title handwriting is still far too slow for the desired experience;
compiler optimization alone is insufficient. Profile its per-stroke shadow blur
and compositing path before enabling it. This report does not establish the
cause of the bottleneck.

Remaining release measurements include actual playback FPS, per-seek p95,
60-second exports, thermal/memory behavior, interruption/recovery, visual parity
on the device, and additional device coverage. All rollout flags remain off.
