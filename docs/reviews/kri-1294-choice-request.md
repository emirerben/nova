# KRI-1294: choice labels are not creator instructions

Parent incident: KRI-1293. Production thread `2113c778`, job `fba2c864`,
2026-11-07, build 1919, protocol 44. The Q3 prompt asks for one silent 11-second
clip, the exact Claude-session line in small white TikTok-style text in the
middle throughout, no captions, no music, and no trimming.

The first draft held the whole-clip ask. After “Make it without these”, the
server correctly superseded r3, but the raw button label appeared both in the
chronological request and as the latest message. The composer rejected the
unresolved “these”. Read-only events and job diagnostics independently confirm
this. Raw diagnostics, source references and signed URLs remain outside git.

## Acceptance and fix

A structured simpler-version answer keeps its UI echo and named omission
consent, but models and the approval binding read a neutral acknowledgement.
Typed text is never filtered by matching the label. The current brief and its
superseded-requirements block remain the render authority. Existing approved
bindings are immutable and are not rewritten; a new approved generation is
needed for the original failed job.

`services/song_order.model_text` already normalizes structured clip/song/mark
answers. It now also normalizes a structured simpler-version answer in EN/TR.
Both binding inputs use that same function rather than raw event content, so
Main Creator, clip planning and text composition agree. No new model call,
prompt template, schema, flag or consent rule is introduced.

## Evidence

Before the fix, the original Q3 provider-replay journey on `899098e4e` failed
at dispatch because the pinned request contained the button label twice.
EN/TR controls also failed; ordinary typed-text controls passed.

The committed regression preserves that historical hold by setting the existing
`keep_whole_take_plan_enabled` switch off. KRI-1295 separately makes the normal
one-clip path approvable without dropping r3. The real planner, runtime,
readiness, choice matching, server omission, approval, dispatch, original-source
binding, montage planner, composer and native compiler run over scratch Postgres.
Only provider responses are authored. Production provider I/O was redacted, so
these are not recorded model outputs or proof of fresh model behavior.

Button and typed-alias paths pass. The binding retains r1/r2/r4/r5 and records r3
as superseded. A genuine unmet composer response still fails. Typed text matching
the label remains unchanged. Related choice, binding, earlier simpler-version,
clip-picker language, Cunda and composer suites also pass.

macOS AVFoundation native exporter, 2026-11-07 21:38 UTC: the compiled recovery
recipe exported a 4.5-second 1080×1920 video from source 5.0–9.5 seconds. Three
sampled frames retain the exact requested line in the middle, in white TikTok
Sans. PCM audio is silent. A no-text twin export is detected by frame comparison.
The sampled middle frame was visually inspected. Safe measurements and hashes:
`kri-1294-native-report.json`. This uses synthetic original bytes whose identity
is pinned before approval; it is not the original Pexels footage or an iPhone run.

## Repeat

```bash
bash scripts/dev/test-db.sh
bash scripts/check-api.sh test tests/kria/test_kri1294_choice_request_journey.py -q
OUT="$PWD/.dev/q3-native"
mkdir -p "$OUT"
ffmpeg -v error -y -f lavfi -i 'color=c=0x243040:s=1080x1920:r=30:d=11' \
  -c:v libx264 -preset ultrafast -pix_fmt yuv420p "$OUT/clip-1.mp4"
KRIA_Q3_EXPORT_DIR="$OUT" bash scripts/check-api.sh test \
  'tests/kria/test_kri1294_choice_request_journey.py::test_q3_hold_choice_approval_composition_has_no_button_instruction[button]' -q
KRIA_AUDIO_PARITY_DIR="$OUT" swift test \
  --package-path src/apps/ios/Packages/KriaMediaEngine --filter AudioParityFixtureTests
src/apps/api/.venv/bin/python scripts/ios/kri-1294-choice-recovery.py "$OUT"
```

Native output/recipe mtimes must belong to the same run. Sandbox restrictions may
require permission to run Swift's existing nested package sandbox. Nothing here
writes production, calls paid providers, merges or deploys. Original-footage,
physical-device, fresh-model and deployed verification remain with KRI-1293;
the full no-trim acceptance is KRI-1295, not claimed by this recovery result.
