# KRI-942: turn clip sound on and off where the creator says

Parent incident: KRI-938 (Don Julio walk-in M1, 2026-10-25, thread `33BA8AFB`, jobs `1cb33402`
and `b3820ddc`). Sibling: KRI-939 (title on the speech-excerpt route, PR #1839).

## The incident, from the raw records

- Turn `a3e6811b` (the original request) wrote `montage_audio = {source_media_ids: [Atakan's
  clip], preserve_source_audio: true}` with `voice_mode` null. The pinned contract therefore
  required camera speech, the job took the speech-excerpt route and was refused for the title
  (KRI-939).
- Turn `055a33ea` ("add the title") re-proposed the whole strategy with `source_media_ids: []`
  and `voice_mode: "excerpts"`. Its rationale still claimed "only your friend's 'Super excited!'
  dialogue punches through the otherwise muted video". The job took the unified montage route,
  where `phone_guided_plan` builds ONE global `AudioMixRecipe(original_volume=…)` from
  `plan_preserves_source_audio`. Every clip played its own sound.
- The brief carried the ask the whole time: `r7 [audio/global] "Clip sound off except that last
  line."`, `facts: {}`. With no facts, no checker could judge it: the receipt said "I can't
  verify this one automatically yet."
- Ground truth (test kit README, reviewed source observations): "Super excited!" sits at
  ≈5.9–6.7 s inside M1_04 (`15_atakan_plaque.mp4`). Render 2 cut that clip at 5.71–6.74 s.

So the meaning was recorded only in the per-turn strategy, which every turn rewrites, and the
montage renderer had no way to play some clips and not others, let alone part of a clip.

## Acceptance (what the creator hears)

1. "Clip sound off except that last line" on a phone montage: every clip silent except the
   closing clip's "Super excited!", which plays whole. Still true after a correction turn that
   doesn't touch sound ("add the title").
2. "Mute the last clip" / "son klibin sesini kapat": the last clip is silent, the rest keep
   their sound.
3. "Only keep the sound on the butcher clip" / "sadece kasap klibinin sesi kalsın": that clip
   audible (every cut of it), the rest silent.
4. "No sound from 5 to 8 seconds" / "5. saniyeden 8. saniyeye kadar ses olmasın": silent in
   that stretch of the finished video, sound elsewhere.
5. "Mute the first 2 seconds of the grill clip": silent in that part of that clip.
6. The receipt says what the finished video does ("met") or plainly why not ("Couldn't …"),
   judged against the recipe the phone actually renders, never "I can't verify".
7. A part of a clip the phone can't mute yet (timed windows switched off) is said before and
   after, never silently skipped; it never costs the creator their video.

Still wrong even if the above pass: a muted stretch that cuts a word in half; a "met" receipt
when the pinned recipe plays the sound; the editor preview disagreeing with the export (known
gap, below).

## Ways creators ask (case matrix)

| Ask | EN | TR | Target | Expected on the final timeline |
| --- | --- | --- | --- | --- |
| A | Clip sound off except that last line | Son cümle hariç klip sesi kapalı | global off, except last clip's line | silent everywhere except the line (padded) |
| B | Mute the last clip | Son klibin sesini kapat | last clip | last clip silent |
| C | Only keep the sound on the butcher clip | Sadece kasap klibinin sesi kalsın | named clip on, rest off | every cut of that clip audible, rest silent |
| D | No sound from 5 to 8 seconds | 5. saniyeden 8. saniyeye kadar ses olmasın | edit window | silent 5–8 s |
| E | Mute the first 2 s of the grill clip | Izgara klibinin ilk 2 saniyesinde ses olmasın | clip + window | silent in that clip's first 2 s |
| F | Keep the sound only when she says "Merope" | Sadece "Merope" dediğinde ses olsun | spoken line on, rest off | audible only around the line |
| G | (correction) Add the title | Başlığı ekle | none | sound plan unchanged |
| H | Sound on everywhere again | Sesi her yerde aç | global on | everything audible (later ask wins) |

Unclear wording ("turn the sound down at the end", "the noisy part") records no on/off fact; the
brief keeps the words and the receipt stays neutral. "Lower / quieter" is a level change, not
on/off, and is out of scope here.

Excluded: voiceover and Talking edits (they have their own clip-sound asks, KRI-842/KRI-841);
there the receipt says plainly that on/off by clip or seconds works on a phone montage. Cloud
renders: Kria scope is iPhone only.

## Failure modes (written before the code)

1. The ask is lost on a correction turn (the strategy is re-proposed each turn).
2. The ask lands on the wrong clip ("last" read in attachment order, not the edit's order).
3. A window shifts: trims, refits and transitions move clips between plan and recipe.
4. A muted window cuts through a spoken word, or the kept line is clipped at an edge.
5. A click at a mute edge (no ramp).
6. The receipt says "met" while the pinned recipe plays the sound (or the reverse).
7. The phone can't do part-of-a-clip muting (flag off, old build) and the render fails instead
   of degrading, or degrades silently.
8. The named line isn't in the clip / is said twice: a guessed window.
9. A window past the end of the video, or a zero-length window.
10. Two asks conflict (global off, later "sound on in the last clip"): a non-deterministic
    winner.
11. The speech-excerpt route ignores the ask while the receipt claims it.
12. An editor Save rebuilds the recipe without the mutes and the receipt keeps saying "met".
13. Text changes the rule: Turkish or reworded asks miss a regex (KRI-667: no word matching).

## Design

**Meaning lives in the brief (1, 13).** The Main Creator (and the narrow brief extractor, which
shares the brief section) records each on/off ask as an `audio` requirement with typed facts,
English keys, whatever the language:

```json
{"clip_sound": "off" | "on",
 "sound_clip": "first" | "last",          // or scope "clip:<media_id>"
 "sound_from_s": 5, "sound_to_s": 8,      // seconds of the video, or of that clip when one is named
 "triggers": ["Super excited!"],          // only where that line is said
 "sound_except": [{"sound_clip": "last", "triggers": ["Super excited!"]}]}
```

The brief is cumulative and versioned, so a turn that doesn't touch the requirement keeps it.
`clip_sound` already carries `under_voice` / `after_voice` for voiceover edits (KRI-842); the new
values never match that reader (`app.kria.narrated_clip_sound_ask`) and vice versa.

**Resolution on the real clock (2, 3, 8, 9, 10).** `app.pipeline.clip_sound_toggle` (pure)
resolves the asks against the compiled phone recipe's clips (the device's own timeline, after
refits): "last" is the last picture clip of the edit, a named clip is every cut of it, a line is
grounded in that clip's timed speech (`phone_clip_sound.ground_trigger_line`, the KRI-842
grounding) and mapped through every cut that holds it. Precedence is fixed: the base (every
clip's own sound, or only the strategy's named sources), then whole-video asks in brief order,
then targeted asks in brief order; inside an ask its `sound_except` parts override its main part.
A line that isn't found, is ambiguous, or isn't in the edit resolves to nothing, with a reason.

**Building it (4, 5, 7).** In `_run_phone_guided_job` (every phone montage that renders through
the guided compiler, the unified montage included), like the timed-cards lane: compile once,
resolve against that recipe, set `source_audio_muted` on moments silent end to end and
`source_audio_mute_spans` (source seconds, so a later trim keeps the same footage) on moments
silent in part, then compile again. Part-of-a-clip spans compile to recipe `mute_windows`, which
the engine ramps over 25 ms at each edge (`applyAudioGain`). Spans are padded outward around a
kept line (a word is never cut) and snapped to the moment edge within 0.25 s. Part-of-a-clip
muting needs `PHONE_CLIP_SOUND_WINDOWS_ENABLED` (default **false**) and `visualBlocks` verified;
off, a partial "off" part is not built and a partial "on" part keeps the whole clip audible (the
line is heard), each said in the receipt. A compile that refuses the spans is retried without
them; the job never fails because of a sound ask.

**Strategy sources on the unified route.** `montage_audio.source_media_ids` (with
`preserve_source_audio`) now means what the prompt says on the unified route too: only those
clips keep their sound. It is the base the brief asks apply on top of.

**Receipts against the pinned recipe (6, 11, 12).** The receipt re-resolves the asks against the
recipe the job actually pinned (or the editor Save re-pinned) and reads each clip's real level
(`volume`, `original_volume`, mute windows, audio-track clips of the same source): "met" only
when every "off" stretch is silent and every "on" stretch is audible. That covers the
speech-excerpt route and editor Saves without trusting the planner. The planner's record
(`variant["clip_sound_toggles"]`) only supplies the reason when something could not be built.

**Correction turns (1).** On top of the durable brief, when the brief holds a sound ask and a
turn's `brief_updates` leave every `audio` requirement unchanged, the draft's `montage_audio` and
`voice_mode` are kept if the model rewrote them, as long as the named clips are still in the edit,
so the route doesn't flip between turns. A brief with no sound ask leaves the planner free
(KRI-945's spoken-excerpt montage picks its camera voice on a text-only turn).

## Evidence

**Offline replay (incident).** `tests/tasks/test_kri942_clip_sound_toggle.py` runs the incident's
own approved execution plan and clip records (`tests/fixtures/kri942_donjulio_walkin_m1.json`,
ids renamed m1_01..m1_08, no paths or user ids) through the real dispatcher, line grounding,
phone compiler and pilot check. With r7 recorded as the prompt now teaches, cuts 1-6 pin at
volume 0 and Atakan's cut plays whole; r7 is "met". The as-recorded brief (r7 without facts)
pins today's all-audible recipe, and the judge rejects that recipe (17 s of mismatch). Wrong
content control: the same recipe with the flowers clip audible and Atakan muted is rejected.
The turn-2 strategy (`[]`, "excerpts") carried over turn 1's `[m1_04]` gives the same pinned
contract audio sources as turn 1. Every case-matrix row (A-H) and failure mode 1-4, 6-11, 13 has
a test. Expectation source for the line: the kit README (5.9-6.7 s from the source video) and the
clip record's own segment (5.86-6.44 s).

**Native output (KriaMediaEngine on macOS, the iPhone engine's exporter).** The three pinned
recipes above (`fixed_off_except_line`, `incident_all_audible`, `window_5_8`) were exported with
the kit's real footage (`~/Downloads/kria-julio2-test/upload/M1_walk_in_montage`, text layers
removed) through `AVFoundationLocalExporter` from a temporary engine test, then measured with
ffmpeg (mono 48 kHz RMS; `silencedetect=noise=-50dB:d=0.1`):

| Export | Stretch | Level | silencedetect |
| --- | --- | --- | --- |
| fixed | cuts 1-6, 0.05-16.92 s | -120 dBFS (digital silence) | 0.000-16.974 s |
| fixed | "Super excited!", 17.12-17.70 s | -23.0 dBFS | |
| incident (today) | cuts 1-6 | -22.8 dBFS | none |
| window 5-8 | 0.05-4.95 s / 8.06-17.95 s | -25.2 / -21.7 dBFS | 5.002-8.001 s |
| window 5-8 | 5.06-7.94 s | -120 dBFS | |

Each mute edge ramps over about 25-35 ms (5 ms bins fall from -22 dBFS to -120 dBFS between
4.985 and 5.035 s; the largest sample step at the edge is below the typical step nearby), so no
click. The engine's own `AudioEdgeEnvelopeTests.testMuteWindowHoldsSilenceAndDeclicksItsEdges`
covers the same contract in preview and export. This is a macOS export, not an iPhone run.

**Not checked.** No paid model run: whether the live Main Creator writes these facts for every
phrasing is unverified (the prompt carries EN and TR examples). No iPhone device run. The final
check is Don Julio walk-in M1 re-sent on the iPhone after deploy (needs KRI-939, PR #1839, for
the title on the speech-excerpt route the carried turn takes).

## Known gaps

- The in-app editor preview does not show part-of-a-clip mutes (the editor compiles its own
  preview), and an editor Save drops them; the receipt then says the cut changed. Whole-clip
  mutes are per-segment `muted` and round-trip.
- The speech-excerpt route doesn't apply the asks; its receipt is judged from its recipe.
- Live model behaviour (does the model write these facts for every phrasing?) is unchecked: no
  paid runs. The prompt examples cover EN and TR.
