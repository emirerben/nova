# iPhone 13 Pro text-effect pilot — 2026-09-11

**18/18 cases completed preview frame requests and six-second H.264 exports on
physical hardware. This is a pilot, not the KRI-29 release gate.**

Device: iPhone 13 Pro (`iPhone14,2`), iOS 26.6.1. Signed Debug app, bundle
`com.kria.app.dev`. Recipes are 1080×1920, 30 fps, six seconds, using bundled
footage and fonts. The backend compiler generates the bundled catalog; the app
uses `AVPlayerPreviewComposer` and `AVFoundationLocalExporter` without a server.

Each case requests compositor frames at 0.1, 1, 3, 5.3, 1 and 5.8 seconds before
exporting. This exercises a backwards jump. Export duration is verified. A
contact sheet of the first 17 phone exports at two seconds was visually inspected.
The source demo video already contains a Corfu label; that label is not generated
by the native text engine.

## Timings and the handwriting fix

| Six-second export | Before | After shadow cache |
| --- | ---: | ---: |
| Handwriting | 53.94 s | 2.49 s |
| Dissolve | 5.99 s | 5.91 s |
| Staggered slice | 3.61 s | 1.45 s |
| Other cases | 1.09–1.62 s | 1.09–1.61 s |

Handwriting re-blurred every completed pen stroke on each partial frame. Retaining
its CIContext alone did not improve the result (53.74 seconds). Caching completed
stroke shadow tiles reduced the measured export time by about 22×. Only a growing
stroke needs a new shadow; cache tiles count against the composition's aggregate
bitmap budget. When no cache space remains, rendering uses the original path.

`HandwritingTests.testShadowCachePreservesPixelsAndBackwardSeekingWithinBudget`
compares every pixel against uncached drawing, with rotation, two colored blur
layers, forward/backward time jumps, and a budget that permits no cached tiles.
All 180 decoded video frames from the before, context-only, and cached physical
iPhone handwriting exports have identical frame hashes.

Raw reports: [before](iphone13pro-effects-before.json),
[context only](iphone13pro-context-only.json), [after](iphone13pro-effects-after.json).
Timings are single pilot samples; changes for other effects are not controlled
performance claims. The report records encoder wall time, not total preview setup.

## Repeat and inspect

Generate the catalog from `src/apps/api`:

```sh
STORAGE_BUCKET=test DATABASE_URL=sqlite:///test.db PYTHONPATH=. .venv/bin/python ../../../scripts/generate-device-effects.py
```

Build/sign the Debug app with the device's development team. Launch with
`-device-effects` for the interactive picker, preview controls, scrubbing and
export sharing. Add `-device-effects-auto` to run the full catalog. Reports and
MP4s are saved in the app's `Documents/DeviceEffects` directory. This entry point
is compiled only in Debug builds.

Outstanding: sustained displayed playback FPS, seek-to-display p95, 60-second
exports, memory and thermal measurements, interruption/recovery, a current iPhone,
full style/language combinations, and all other creator lanes. All rollout flags
remain disabled.
