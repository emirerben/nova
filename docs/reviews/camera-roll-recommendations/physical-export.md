# Physical camera-roll execution proof

19 October 2026. Internal synthetic correctness evidence, not a creator-usefulness pilot.

- iPhone 13 Pro, iOS 27.0.1 (24A446), separate `com.kria.app.camerarollqa` app.
- Production AVFoundation exporter, no branding for this isolated adapter check.
- Two synthetic 180s videos with distinct pictures and time-varying audio.
- Frozen draft selects 155–162s from each: 14s total, source sound, no added text/music.
- The database journey verifies separate approval and freezes the reviewed choices. The
  device harness consumes its saved envelope; this does not prove a complete signed-in UI journey.

| Export | Independent result | Checks |
| --- | --- | --- |
| Correct windows/order/sound | checked | 17 |
| Wrong windows | rejected | 17 |
| Muted sound | rejected | 17 |
| Reversed order | rejected | 17 |

[Safe aggregate with checksums and timings](physical-export.json). Detailed recipes,
test identities and synthetic MP4s remain under ignored `.dev/camera-roll-export-proof`.
No real creator media, model calls or publication was used for this proof.

## Repeat

```bash
src/apps/api/.venv/bin/python scripts/ios/camera-roll-export-proof.py synthetic --output .dev/camera-roll-proof-input
KRIA_CAMERA_ROLL_PROOF_INPUT="$PWD/.dev/camera-roll-proof-input" bash scripts/check-api.sh test tests/integration/test_camera_roll_execution.py -q
src/apps/api/.venv/bin/python scripts/ios/camera-roll-export-proof.py prepare \
  --prepared .dev/camera-roll-proof-input/prepared.json \
  --bindings .dev/camera-roll-proof-input/bindings.json \
  --originals .dev/camera-roll-proof-input/originals.json \
  --output .dev/camera-roll-export-proof
```

Build/sign the DEBUG app for a dedicated bundle ID using an existing development identity
and profile, then install on the chosen device. Copy only this synthetic fixture folder
to the QA app's `Documents/camera-roll-export-proof`; launch `-camera-roll-export-proof`.
The screen uses the production native exporter and saves `device-export.json` and
`cases/<name>/phone.mp4`. Copy these synthetic outputs back to the host folder, then:

```bash
src/apps/api/.venv/bin/python scripts/ios/camera-roll-export-proof.py compare --output .dev/camera-roll-export-proof
```

Require `pass: true`: one checked output, three rejected controls. New encodes have new
checksums; compare verifier outcomes, not byte equality with the retained MP4. Operator
inputs alone are not authenticated evidence; retain the database result with the device report.
