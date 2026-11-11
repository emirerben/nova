# Reproducible native text-motion graphemes

Main CI run [42289925815](https://github.com/emirerben/nova/actions/runs/42289925815/job/128141152150), at `11310a4ea5ede7df030d2a4d3df9de8a96c54cb4`, failed only `tests/kria/test_phone_text_motion.py::test_native_text_motion_fixture_matches_python_renderer`. Its Hindi Smooth Type case expected a 0.6-second settle and produced 0.6622641509433962 seconds.

The unconstrained `regex>=2024.11` dependency selected `2026.11.7`, whose distribution metadata declares Unicode 18. It segments `नमस्ते दुनिया` into eight clusters, splitting `स्ते` into `स्` and `ते`. `regex==2026.6.28` declares Unicode 17 and produces seven clusters, matching Swift Character segmentation and the existing timing oracle. With a 33 ms stagger, 120 ms reveal ramp and speed 0.53, the extra cluster accounts for the entire 0.0622641509433962-second difference.

The API now pins the qualified version. This fixes installation reproducibility for API and render workers, which both install from the API pyproject. It does not change animation equations, loosen floating-point tolerances, regenerate the timing oracle, or introduce language-specific segmentation code.

## Acceptance and evidence

The existing strict shared timing fixture remains unchanged. Python and Swift additionally assert the same explicit grapheme sequences for Hindi, plain and ZWJ Indic conjuncts, emoji with skin tone and ZWJ, decomposed accents, and flag pairs. The ZWNJ case must remain split as a negative control.

An isolated install of `regex==2026.11.7` reproduces the CI failure and fails the three conjunct guards. The existing qualified installation passes the Python oracle. A clean temporary install of the exact pinned version verifies fresh-install behavior. The native `TextMotionTimingTests` execute the unchanged cloud timing fixture and the new character-boundary contract on macOS.

Reproduction commands from the repository root:

```bash
python -m pip install --target /tmp/kria-regex-unqualified regex==2026.11.7
PYTHONPATH=/tmp/kria-regex-unqualified src/apps/api/.venv/bin/python -m pytest src/apps/api/tests/kria/test_phone_text_motion.py -q
python -m pip install --target /tmp/kria-regex-qualified regex==2026.6.28
PYTHONPATH=/tmp/kria-regex-qualified src/apps/api/.venv/bin/python -m pytest src/apps/api/tests/kria/test_phone_text_motion.py src/apps/api/tests/schemas/test_clip_intents.py src/apps/api/tests/pipeline/test_text_overlay_skia.py -q
swift test --package-path src/apps/ios/Packages/KriaMediaEngine --scratch-path .dev/swift-unicode-build --jobs 2 --filter TextMotionTimingTests
```

The first pytest command is the intentional red control. Native parity is qualified for the tested Swift runtime; its Unicode implementation can also change. Future dependency or native toolchain upgrades must pass both suites before changing this pin. These checks establish segmentation and timing parity, not an inspected iPhone export or production rollout. No paid model or provider call is required.
