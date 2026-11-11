# KRI-720 — speech cleanup for Talking edits made of several takes

Status: implemented behind `TALKING_MULTI_TAKE_CLEANUP_ENABLED` (off), native export verified on macOS KriaMediaEngine with Çiğdem's real S3 takes (2026-11-09). iPhone run pending deploy + flag. Direction picked by Emir: full fix (per-take cleanup, S3 included), verified on a real iPhone.

## Original request (Çiğdem S3, prod thread cc0e3706)

> There are 3 videos, two at home and one in the car, join them in that order. Cut the pauses, the "okay"s and the places I restarted.

Today (main ff332979f): `resolve_active_narration_source` returns `ambiguous_embedded_source` for a `subtitled` item with 2+ clips that carry audio, so no preflight runs, no "Clean up speech" choice is offered, the job ships `off_v1`, and since KRI-1327 the draft says "Couldn't" up front. Reproduced offline on the S3 take lengths (20.1 s / 7.0 s / 7.2 s): three takes → `ambiguous_embedded_source`; single-take control → resolves.

## Acceptance (what the creator sees/hears)

1. A Talking edit with several takes offers "Clean up speech" before approval with counts that cover every talking take.
2. After approval, each talking take plays with its own pauses/fillers removed; takes still play in the approved order.
3. No real word is lost (re-transcribed export vs source takes).
4. No pause > ~0.4 s left inside speech, including at the joins between takes.
5. Captions sit on the spoken words (cue start within ~0.1 s of the word in the export).
6. Full-screen photos / footage cutaways land on their trigger word in the cut timeline, never on removed audio.
7. Editor Save keeps every take's cuts (no take reverts to uncut).
8. Flag off: byte-identical to today (still "Couldn't").

Still wrong if: only the first take is cut; a take's cut lands on another take; captions or photos drift by the removed time of earlier takes.

## Failure modes (written before code)

Source selection / consent
- F1 Speaker set at preflight differs from the worker's speaker set (worker classifies by whisper word density) → cuts applied to footage, or a speaker plays uncut.
- F2 Footage clip with ambient audio counted as a take → inflated public counts, or footage "cleaned".
- F3 Take reordered / replaced / added after consent → stale analysis applied to the wrong take. (Consent identity is the ordered footage list: `reconcile_consent`.)
- F4 One take's analysis fails while others succeed → partial cleanup shipped as "done".
- F5 Public counts summed wrongly / a take with no findings hides the others.
- F6 Fingerprint does not include every take → a changed take keeps a stale analysis.

Render
- F7 Take boundary: the last kept segment of take N and first of take N+1 joined with a pause that neither take's plan sees (silence at end of N + start of N+1).
- F8 First/last word of a take clipped by a keep-segment edge.
- F9 Empty keep segments for one take (whole take removed) → zero-length speaker.
- F10 Overlapping / unsorted keep segments after merging opening-cut / explicit cuts per take.
- F11 Extra speakers' timeline offsets still computed from uncut durations → every later caption, beat and cutaway shifted by the removed time.
- F12 Caption words of take N+1 remapped with take N's plan.
- F13 Full-screen photo triggers (KRI-1327 multi-speaker beats) grounded on uncut word times.
- F14 Footage cutaways scheduled on the uncut length → cutaway past the end or on removed audio.
- F15 Ending clip (KRI-841, footage last on main track) mistaken for a speaker and cut.
- F16 Insert clips (KRI-1151) or mention cutaways interplay with per-take cuts.
- F17 Audio declick handles / crossfades at cuts across a take join.

Editor
- F18 `_previous_keep_segments` only reads the spine → saving in the editor un-cuts the other takes.
- F19 Editor timeline offsets for extra speakers assume full durations.

Rollout
- F20 Flag off not byte-identical (single-take path touched).
- F21 Old iPhone builds can't play a per-speaker keep-segment recipe → needs a protocol/capability gate.

## Negative controls
- Wrong-take control: swap take B's plan onto take C (same counts) → the word-diff / caption check must fail.
- Flag off → still `ambiguous_embedded_source` and "Couldn't".

## Verification plan (no paid model runs except the approved iPhone chat runs)
- iPhone export of S3 + one single-take control.
- `ffmpeg -af silencedetect` on the export: no gap > 0.4 s inside speech.
- Local whisper.cpp of the export vs source takes: zero missing real words.
- Caption cue times vs export word times ≤ 0.1 s; photos on trigger words.
- Kit checker: `~/Downloads/kria-cigdem-test/tools/check_result.sh <video> S3`.

## Native export proof (2026-11-09, macOS KriaMediaEngine, real S3 takes)

What ran (no chat, no DB, no storage, $0): the REAL preflight engine per take (whisper.cpp
large-v3-turbo standing in for prod whisper-1; the two restarts marked by listening stand in for
the paid retake detector, `tests/fixtures/kri720_s3_take_observations.json`), the REAL several-take
assembly (`_analyze_takes`), `analysis_snapshot` -> schema-2 job snapshot, the REAL worker
(`_run_phone_subtitled_job`, `required_v1`), and the recipe exported by KriaMediaEngine's
production exporter (`AudioParityFixtureTests`, the same AVFoundation code as the phone).

```bash
cd src/apps/api && PYTHONPATH=$PWD <api venv>/python ../../../scripts/dev/kri720/s3_proof.py ~/Downloads/kria-cigdem-test OUT
(cd src/apps/ios/Packages/KriaMediaEngine && KRIA_AUDIO_PARITY_DIR=OUT swift test --filter AudioParityFixtureTests)
python3 -I scripts/dev/kri720/measure.py OUT
<api venv>/python -I scripts/dev/kri720/audio_align.py OUT ~/Downloads/kria-cigdem-test
```

Cases (recipes: `scripts/dev/kri720/s3_recipes.json`): `s3_clean` (A, B, C, cleanup), `s3_clean_a_last`
(B, C, A: the cut take is a LATER speaker), `single_clean` (A alone, today's path), `s3_uncut`
(control, cleanup off).

| Check | s3_clean | s3_clean_a_last | single_clean | s3_uncut |
|---|---|---|---|---|
| Export length = recipe | 32.329 = 32.329 | 31.922 = 31.922 | 18.188 = 18.188 | 34.249 = 34.249 |
| Real words lost (whisper.cpp of export vs kept words) | 0 / 90 | 0 / 90 | 0 / 49 | 3 / 94 (transcriber hears the brand names differently) |
| Caption word vs its own sound in the export (cross-correlation, r >= 0.6) | 87 / 89, max lag 0.000 s | 63 / 89, max lag 0.005 s (the other 26 also sit at 0.000-0.005 s, r 0.4-0.59) | 47 / 48, max 0.000 s | n/a (raw whisper vs adjusted analysis times) |
| Pauses >= 0.4 s left inside speech | 5 (0.42-0.56 s) | 5 (0.42-0.56 s) | 3 (0.42-0.55 s) | 6 (0.42-0.63 s) |
| Pause at a join between takes | none | none (was 0.61 s before `_trim_take_joins`) | n/a | n/a |

Negative control: the `s3_clean_a_last` captions searched in the UNCUT export: 0 of 89 words
correlate (`scripts/dev/kri720/s3_audio_align.json`); transcriber timing alone would put them
~20 s off (`s3_report.json`). Full numbers: `scripts/dev/kri720/s3_report.json`.

Findings:
- Per-take cutting, joins, the later take's offset and captions are exact on real media.
- F7 found and fixed by this proof: each take's cleanup sees only its own pauses, so take C's
  tail and take A's lead-in (after the slate cut) met as a 0.61 s pause. `_trim_take_joins` keeps
  `TAKE_JOIN_KEEP_S` (0.15 s) of silence on each side of every join between cut takes.
- Take A's slate ("Wait, the light? Okay.", 0-1.92 s) is cut. The second restart (14.13-17.49 s)
  was proposed and then dropped by the existing filler relisten guard (`filler_check.relisten`
  "lost" words) on whisper.cpp's loose word stamps: unchanged single-source engine behaviour, the
  same in `single_clean`; prod uses whisper-1 + the retake detector.
- NOT met: Çiğdem's format rule "no pause > 0.4 s". Default cleanup removes pauses >= 0.6 s
  (long-pause pass) for every take; 0.42-0.61 s pauses stay, exactly as on a single take. Tight
  0.3 s pauses run only on an explicit ask and only on a single take (follow-up).
