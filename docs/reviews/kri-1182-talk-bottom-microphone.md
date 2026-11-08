# KRI-1182: Talk to Camera frame-rate buzz and the bottom microphone

Durable summary of a device bench run. The raw takes and `report.json` files live in the gitignored
`.talk-bench/` folders on the author's machine, so this file carries the numbers. Every number below was
MEASURED on one iPhone 13 Pro (Debug build) through `scripts/ios/talk-bench.sh`, scenario `none-cin0`, 20 s
silent takes. Statements marked INFERRED are interpretation, not measurement.

## Report

The founder reported that Talk to Camera takes make a "trrr" sound, "like a money counting machine".

## Acceptance (what the creator hears)

- A Talk to Camera take has no constant buzz at the video frame rate.
- Headsets, Bluetooth and wired inputs keep working exactly as before.
- A phone without a bottom microphone data source keeps its route unchanged.

## Root-cause narrowing (measured)

- Stock Camera control take (stereo, 24 fps): no pulse on either channel (30 Hz modulation index 0.03).
- Kria takes pulse at the video frame rate: peak at 30.0 Hz at 30 fps; forcing 24 fps moved the peak to 24.0 Hz.
- Forcing HDR video off did not remove it.
- The bench script's 30 fps redraw was refuted as the cause (off, or 15 fps: the pulse neither went away nor moved).
- The app's default microphone was already the FRONT microphone (route reported "Frente/Front"), beside the front camera.
- The founder listened to takes recorded with the front, back and bottom microphones: only the front-microphone take has the "trrr".
- INFERRED: the front microphone picks up the front camera's per-frame electrical noise.

## Final interleaved A/B (measured, 3 takes per arm, alternating, quiet room)

Metric: 30 Hz modulation index of the audio envelope.

| Arm | Route | Take 1 | Take 2 | Take 3 |
| --- | --- | --- | --- | --- |
| Without fix (`KRIA_TALK_BENCH_MIC=app`) | front | 0.387 | 0.322 | unusable (loud room, speech masks the metric) |
| With fix | "Base/Bottom" | 0.016 | 0.016 | 0.017 |

- Without the fix the two usable takes show a 30.0 Hz peak at 374x and 274x the median modulation power.
- With the fix there is no 30 Hz peak; the strongest modulation is the 8 Hz speech rhythm. About 20x lower.
- A 9-16 kHz re-measure agrees: ratio versus neighbouring frequencies 5.7 and 5.6 without the fix, 0.8 to 1.3 with it.

## Cost (measured) and limits

- The bottom microphone's quiet-window noise floor is higher: -53, -52, -50 dBFS with the fix versus -57, -59 dBFS without (third take excluded), about 5 to 7 dB.
- NOT measured: voice level and intelligibility with the bottom microphone (nobody spoke in the bench takes); wind and handling noise.
- Only an iPhone 13 Pro was tested. Other models' microphone layouts are unverified; the code leaves the route unchanged when there is no bottom source.
- No Release telemetry change. The Debug-only `KRIA_TALK_BENCH_MIC=app` lever exists only for this A/B.
