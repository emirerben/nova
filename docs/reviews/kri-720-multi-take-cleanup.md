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
- F21 Old iPhone builds preview the later takes uncut (the render itself is plain clips and plays) → old builds must show the finished video.

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

## Follow-up: the four gaps (2026-11-09, second pass)

Decisions:
- G1 tight pauses: on a video made of several takes, the tight pass (every pause down to ~0.3 s)
  runs for a plain "cut the pauses" too, not only for "cut ALL my pauses" (Yasin, 2026-11-09:
  Çiğdem's real S3 message says plain "Cut the pauses" and her format allows no pause > 0.4 s).
  One-take videos keep today's rule (plain = pauses >= 0.6 s). Joins between takes are always
  trimmed to the same tight target, with word stamps + the voice guard.
- G2 "oh sorry, again": reproduced with the live whisper-1 words of job 7416c94b (caption cue
  words, raw source times). The relisten guard (`_relistened_plan`) re-listens any removal that
  overlaps a lexical filler ("oh"), and judges every word inside the cut; a restart's abandoned
  words ("sorry", "again", even the redelivery "and" via pool matching) are lost by design, so
  the restart was dropped: `relisten.dropped=[{13.94-17.54, lost: [sorry, again, and]}]`.
  Not a loose-stamp problem. Fix: a restart removal is judged only on the words just OUTSIDE its
  own word span (by word index, aligned times when the aligner ran), so the safety net still
  catches a cut that eats a neighbour word.
- G3 named line in several takes: grounded in each take's own preflight words; cut only when
  exactly one take (and one place in it, or the creator's pick) says it; then the joins/offsets/
  captions/photos follow from that take's plan.
- G4 older app builds: prod jobs of 2026-10-26..11-09 were approved on client protocols 22..46
  (62 jobs; only the last two days on 44-46). The several-take render is plain timeline clips, so
  old builds export it; the hole is the editor PREVIEW (old builds cut only the first take, so
  takes B/C preview uncut and captions/cards/sounds sit on the wrong words). A protocol gate would
  lock out every build in use, so: protocol 47 = understands `speaker_removed`; callers below 47
  (or with no protocol) get the variant served so they take their existing finished-video
  fallback; Save keeps every take's cut either way. F21 below is reworded accordingly.

Failure modes written before code:

G1 tight pauses on several takes + joins
- T1 a tight ask on several takes gets LESS than plain today (long pass skips tight briefs,
  tight pass skips several takes) -> every take must get the tight pass.
- T2 the join trim cuts a soft word ending / onset (no word stamps, no voice guard, keeps 0.15 s
  < the tight pass's attach/soft-ending margins).
- T3 join gap = tail kept + lead kept; 0.15 + 0.15 = 0.30 s sits on the limit -> use the tight
  pass's pad per side.
- T4 clip budget: each kept segment is a clip on one shared main track (<= 100); three tight
  takes can exceed it -> shared budget across takes.
- T5 a noisy take gets no tight cuts but the draft promised ~0.3 s -> per-take "noisy" surfaced.
- T6 a measurement failure on one take silently summed away.
- T7 captions/photos/cutaways: tight cuts must land in the take plans before words are remapped.
- T8 receipts/counts only see the first take (`silence_cut`, `tight_pauses`) -> "found no
  pauses" while later takes were cut; finished-video review must read every take + joins.
- T9 draft says "only the long pauses" for several takes while the render cuts tight (or the
  reverse); Turkish twin missing.
- T10 non-adjacent joins: an uncut take (no speech / ending clip) between two cut takes.
- T11 flag off / one take: byte-identical.

G2 restart kept by the relisten guard
- R1 exempting restarts entirely removes the only acoustic net: a wrong restart span would cut
  real speech -> keep relistening, judge only the neighbours.
- R2 word index vs time mismatch after alignment re-timing (zero-length whisper-1 words).
- R3 left edge eats the tail of the word before (whisper-1 late stamps) -> neighbour judged.
- R4 right edge leaves 0.12 s before the redelivery; a quiet onset could be clipped.
- R5 aligner unavailable / non-English -> raw stamps; behaviour must stay safe (neighbour check
  still runs on the relisten transcript).
- R6 pool matching mismatches repeated words ("and ... and") -> judge by index window.
- R7 a filler-only removal must still be judged exactly as today (no regression in the guard).
- R8 paid budget: relisten calls per take multiply with several takes (RELISTEN_MAX_CUTS).

G3 named line in several takes
- N1 the line is said in two takes -> refuse (no guess), receipt names it.
- N2 said twice in one take with no pick -> refuse as today (`said_more_than_once`).
- N3 never said -> `not_found`, honest receipt.
- N4 transcriber spelling differs -> no fuzzy for 1-2 word quotes (unchanged).
- N5 cut touches a neighbour word -> midpoint clamps (unchanged) + join re-trim after the cut.
- N6 wrong take: words of take 1 used for a line in take B (today's spine-only call) -> each
  take's own words, keyed by clip id / storage path.
- N7 later takes, captions, photos, cutaways not shifted -> cut must be in the take plan before
  remapping.
- N8 editor Save reverts the cut / preview shows it uncut.
- N9 start/end quotes in several takes stay refused (out of scope) and say so.
- N10 cleanup declined -> no per-take words -> named line still refused honestly.
- N11 flag off byte-identical; draft promise matches the render.

G4 older builds
- O1 an old build still previews uncut (fallback condition not present in every build 22..46).
- O2 the fallback hides something else old builds need (finished video URL, poster, Save).
- O3 old-build Save drops the later takes' cuts.
- O4 new build (47) wrongly falls back; single-take / uncut several-take variants change.
- O5 variants rendered with cuts while the flag is later turned off stay exposed.
- O6 a job approved on 47 reopened on an old device.
- O7 Swift and server protocol numbers drift.

## Proof after the four fixes (2026-11-09, HEAD 172619530, macOS KriaMediaEngine, real S3 takes)

What ran ($0, no model calls): the LIVE whisper-1 word stamps of job 7416c94b stand in for the
server transcriber (one concatenated timeline split by the uncut take durations 20.108 / 6.989 /
7.152 s, split verified by text against the kit's `SCRIPT.txt`); the labelled restarts fixture
(`tests/fixtures/kri720_s3_take_observations.json`) stands in for the paid retake detector; the
relisten stand-in hears exactly the words left in the audio (free, as the G2 repro). Real
engine, real `_analyze_takes`, real worker (`_run_phone_subtitled_job`, `required_v1`), recipe
exported by `AudioParityFixtureTests`. Settings: `tight_pause_cut_enabled`,
`speech_cleanup_long_pauses_enabled`, `talking_multi_take_cleanup_enabled`, retake levers on.
Briefs are bound on the job: Cigdem's real plain ask (`speech_cleanup: true`, no tight fact);
named-line cases add one `cut_lines` entry.

```bash
cd src/apps/api && PYTHONPATH=$PWD .venv/bin/python ../../../scripts/dev/kri720/s3_proof.py \
  ~/Downloads/kria-cigdem-test OUT LIVE_WORDS.json       # LIVE_WORDS = live_caption_words_all_takes.json
(cd src/apps/ios/Packages/KriaMediaEngine && KRIA_AUDIO_PARITY_DIR=OUT swift test --filter AudioParityFixtureTests)
python3 -I scripts/dev/kri720/measure.py OUT
src/apps/api/.venv/bin/python -I scripts/dev/kri720/audio_align.py OUT ~/Downloads/kria-cigdem-test
```

| Case | Export = recipe | Slate + "oh sorry, again" | Named line | Silences >= 0.4 s inside speech | Joins | Real words lost | Caption vs own sound |
|---|---|---|---|---|---|---|---|
| s3_clean (A,B,C) | 27.509 = 27.509 | gone | n/a | 1: 1.96-2.38 (0.42 s) | no gap | 0 (2 transcriber mishears, "lip tint" heard as "Lipton") | 79/84 clear, max lag 0.000 s, 0 over 0.1 s |
| s3_clean_a_last (B,C,A) | 27.509 = 27.509 | gone | n/a | 1: 15.36-15.78 (0.42 s, same pause) | no gap | 0 (same mishear) | 79/84, max 0.000 s |
| single_clean (A) | 14.108 = 14.108 | gone | n/a | 1: 1.96-2.38 (long-only on one take, by design) | n/a | 0 | 42/43, max 0.000 s |
| s3_uncut (control) | 34.249 = 34.249 | kept | n/a | 6 (0.42-0.63 s) | n/a | 0 (2 mishears of "Mira's") | 90/95 |
| s3_named_line ("But that's for the next episode.", take C) | 25.784 = 25.784 | gone | gone; "cafes" and "Don't forget to follow" stay | 1: 1.96-2.38 | no gap | 0 | 73/78, max 0.000 s |
| s3_named_line_tail ("Don't forget to follow.", take C) | 26.349 = 26.349 | gone | gone; "But that's for the next episode" and "cafes" stay | 1: 1.96-2.38 | no gap | 0 | 75/80, max 0.000 s |

Negative control: the `s3_clean_a_last` captions searched in the uncut export: 0 of 84 correlate.
Named-line receipts: `applied`, one occurrence, take 3 (`takeC`), cut 4.10-5.85 s / 5.92-7.08 s.
Numbers: `scripts/dev/kri720/s3_report.json`, `s3_audio_align.json`, `s3_recipes.json`.

Findings:
- G2 holds: the restart "oh sorry, again" (13.94-17.54 s) and the slate (0-2.4 s) are both cut;
  `relisten.dropped=[]` on take A.
- G3 holds: the named line is cut from the one take that says it, later clips and captions shift
  exactly, neighbour words survive.
- NOT met, narrowly: "no pause > 0.4 s". One pause stays in every several-take case: take A
  source 4.39-4.78 s (between "marketing?" and "Because"). The tight pass measures it at 0.389 s
  (-38 dB, high-passed), ffmpeg `silencedetect` -35 dB sees 0.42 s. The pass reports
  `no_change` with `pauses_over_limit: 2`: cutting it to the 0.3 s target would remove ~0.09 s,
  below `TIGHT_MIN_CUT_S` (0.12). Moving the stamp of "Because" later does not change this, so
  it is the min-cut rule, not loose word stamps. Joins are clean (no silence within 0.5 s).
- Caption timing: caption words sit exactly on their own sound in the export (max lag 0.000 s);
  this checks the remap and offsets, the source stamps themselves are whisper-1's.
