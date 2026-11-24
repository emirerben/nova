# KRI-1524 — tight pauses on takes recorded loud (Cusco DJI T1 "Good Take")

Parent run: KRI-1522 (prod job `d988bb99-bf5c-4bc5-9e83-aa2ffae1ca76`, server `80c96b04c`,
build 2016, 2026-11-17). Two Turkish Talking takes from a DJI action camera; the creator asked
"cut my pauses so it's tight" and chose Clean up speech.

## What went wrong

| Step | What Kria did | Why |
|---|---|---|
| Draft | "Choose Clean up speech when you approve and every pause is cut down to about 0.3 s" | no condition; the draft cannot hear the takes |
| Render | `tight_pauses` = `no_change / noisy_take` for both takes, `take_join_gaps` 0.0 s | the gate called any take with a room floor above -45 dBFS noisy; these sit at -40.0 / -40.5 dBFS because the camera records hot (speech is 28.7 / 30.6 dB above the floor) |
| Measure | could not have reported the pauses anyway | the acceptance bar (silencedetect -38 dB, per sample) sits inside a -40 dBFS room's tone: zero pauses found in either take |
| Video | seven gaps of 0.5–1.2 s | nothing beyond the consented cleanup was cut |

## The change

- `tight_pause_cut.take_noise`: a take above -45 dBFS is **noisy only when its speech (95th
  percentile of 40 ms frames) rises less than 25 dB above its floor**; otherwise it is `hot` and
  goes through the same cutter (every cut bar was already floor-relative: quiet check +15 dB,
  in-stamp +10 dB, voice guard +3 dB, fragment and clip-budget rules).
- `relative_silence_spans` / `measure_take_silences`: a hot take's pauses are measured against its
  own floor (90 Hz high-passed, 20 ms RMS at a 10 ms hop, under floor + 10 dB, ≥ 0.12 s). Used for
  the tight / long reports and the several-take join gaps. The fallback join trim (`_trim_take_joins`) cuts with no voice guard, so it keeps the old measure.
- `TIGHT_PAUSE_RELATIVE_FLOOR_ENABLED` (default on): off is byte-identical to before. Takes at or
  below -45 dBFS never change.
- The three 0.3 s draft promises now say "unless a take is noisy" (EN, TR "gürültülü çekimler
  hariç", DE "außer bei starkem Hintergrundlärm"). Drafts stored with the old wording stay render
  promises (`_TIGHT_*_PRE_1524`), so approving an older draft after deploy never holds.

## Gate calibration (299 local kit and fixture files, many duplicates; `_room_floor_db` + 95th percentile)

| Clips (floor above -45 dBFS) | Speech over floor | Verdict with the switch on |
|---|---|---|
| Cusco DJI T1_01 / T1_02 / T2_01 / T3_01 / T4 voiceover | 28.7–31.2 dB | hot (cut) |
| Qendresa V3–V6, Çiğdem R_DfAnY7ZowA7 | 28.4–34.3 dB | hot (cut) |
| Don Julio restaurant, Ecuador café/market talking takes, Çiğdem R_Dem / R_Der | 15.9–24.6 dB | noisy (unchanged) |
| crowd / music fixtures, silent Q3 clip | 3.2–18.1 dB | noisy (unchanged) |
| synthetic wind fixtures wind_03 / wind_04 | 27.0 / 33.0 dB | hot — gusts lift the 95th percentile; their floors (-44.9 / -43.9) sit at the old line, so a copy 1 dB quieter already passed the old gate. Cuts still need silence spans and pass the voice guard |

## Offline replay (`scripts/dev/kri1524_tight_pause_replay.py`, result `kri-1524/replay-report.json`)

Real takes, the run's own consented plans and word stamps, the real worker glue per take
(`_phone_tight_pause_cut` with the several-take arguments, fallback, `_measure_take_join_gaps`),
the kept audio rendered with ffmpeg, and an **independent** gap check (the ticket's method:
voice-band 300–3400 Hz, 50 ms steps, under the file's 5th percentile + 10 dB).

| | Switch off (the incident) | Switch on (the fix) |
|---|---|---|
| Tool on the run's own video | the ticket's seven gaps, 0.5–1.2 s | — |
| Tight reports | both `noisy_take`, 0 cuts | both `applied`: 3 + 6 cuts, guard dropped 0 |
| Join gap | 0.0 s (deaf measure) | 0.29 s (measured) |
| Gaps ≥ 0.5 s (independent) | the same seven, 0.5–1.2 s | none |
| Gaps over 0.35 s (independent) | eight | three: 0.40, 0.45, 0.40 s |
| Length of the two takes | 23.26 s | 20.48 s |
| Finished-video reply | "There's background sound in some of your takes, so I only cut the long pauses in those" | "Speech cleanup cut your pauses tight, but 3 still last longer than 0.35 s (the longest is 0.5 s)" |

The reply counts exactly the three pauses the independent check finds over 0.35 s.

**No word cut.** Every new cut's loudest 20 ms is 0–7.4 dB above the take's floor (speech sits
28–31 dB above it); the 1.2 s pause hidden inside one whisper stamp ("Coricancha'nın", take 2
2.20–3.13 s) is among them. whisper.cpp (large-v3-turbo, local) hears 42 words in both outputs;
the one difference ("bizi" → "biz") is the word after a cut whose loudest frame is +5.0 dB over
the floor, i.e. the same audio heard differently, not a clipped ending.

**Wrong-content control.** Four noisy talking takes (Don Julio restaurant 17.3 dB, Ecuador T2
17.1 dB, Ecuador T1 24.6 dB, Çiğdem R_Dem 22.5 dB) stay `noisy_take` with the switch on, plan
untouched, and the reply says "There's background sound in your take, so I only cut the long
pauses".

## Remaining gaps

- **Measure the shipped export:** the post-deploy iPhone check measures pauses on the final export with the same independent check. With KRI-1523 it plays only the good take, so the two-take replay numbers here are not its expected values.
- **Native export:** not run. The phone applies the server's cut ranges; check after deploy on an
  iPhone with the Cusco T1 kit (KRI-1522).
- **0.3 s is not always reached on a loud take:** the cut sources there are Kria's ambient spans,
  which stay about 80 ms clear of sound, so a few pauses end at 0.40–0.47 s. The reply says so
  with the measured count. A prototype that also cut inside the new measured spans made one take
  fully tight and the other slightly worse; left out.
- **The draft still cannot hear the take:** it states the exception instead of predicting it.
  Warning per take before approval needs the noise verdict in the preflight analysis.
- "Use only the good take" is KRI-1523.
