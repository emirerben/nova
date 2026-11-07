# KRI-1270: speech cleanup leaves long pauses in Talk-to-camera videos

## Report

Emir (2026-11-07) said Talk-to-camera cleanup doesn't cut silences, though it seemed to cut "ıı" fillers.
His example is prod job `c39a3210-fe3c-4ae6-ba0d-ca65fb96b75f`: one Turkish clip, 82.7 s, phone render,
and the creator chose "Clean up speech" (`required_v1`).

## What the job did

- Cleanup was applied: 10 removals, all `silence`, 9.63 s.
- There were no filler candidates. whisper-1 transcribed no filler tokens, so `lexical_candidates` is 0.
  Filler cutting is not part of this report.
- The finished video still has 11 internal pauses of 0.6–1.6 s (`silencedetect=n=-35dB:d=0.5`).

## Root cause

The 136 prod word stamps (`overlay_transcript`, source time) are contiguous: each word starts where the
previous one ends. Every pause therefore sits inside the head of the next word's stamp or the tail of the
previous one.

- Rule 3 of `silence_cut.build_cut_plan` only tightens a pause when the word gap is at least `MAX_PAUSE_S`
  (0.6 s) and silencedetect agrees. Here it sees a word gap of 0.
- Rule 0 refuses edge carves by design (KRI-969, where an edge trim cut a real word).

| Where the pause sits | Pauses |
|---|---|
| Edge of a word stamp | 8 |
| Interior of a stamp, but more than 0.35 s of voice on one side | 3 |

Kria's -30 dB silence spans cover all 11 pauses. The detection was right; the cutter could not see the
pauses through the stamps. This is the same finding as Qendresa 13 (`qendresa-13-tight-pauses.md`: 17 of
21 pauses inside one stamp).

## Fix

When the flag `SPEECH_CLEANUP_LONG_PAUSES_ENABLED` is on, cleanup is consented, the render is a
single-clip phone render and there is no tight ask, the render reuses the tight-pause cutter
(`apply_tight_pauses`) with `min_pause_s=MAX_PAUSE_S`.

- Only silence spans of 0.6 s or more become cut sources.
- Every span still protects sound, so this mode never adds a cut that tight mode would not make.
- Every tight-mode protection still applies: the quiet-floor rule, no change on noisy takes, the voice
  guard, and the fragment and keep-segment caps.
- The receipt goes to `variant["long_pauses"]` (`mode: "long"`, `limit_s: 0.6`). Tight receipts and
  promises are unchanged.
- With the flag off, the plan object is unchanged and the render is byte-identical.

## Offline replay (no paid calls)

The source audio was rebuilt from the finished video's audio by putting the room tone from a kept pause
back into the 10 prod removals. A first rebuild used synthetic -70 dBFS noise. That pulled the quiet floor
down and made every real pause read as "loud", so the room-tone rebuild is the valid one (floor
-58.5 dBFS). The prod plan was then passed through `apply_tight_pauses(..., min_pause_s=0.6)`.

| Measure | Before (prod) | After |
|---|---|---|
| Pauses ≥ 0.5 s at -35 dB | 12 | 2 |
| Longest pause | 1.63 s | 0.62 s |
| Pauses over 0.6 s (receipt) | (longest 1.61 s) | 0 (longest 0.58 s) |
| Video length (with 1.6 s brand tail) | 74.7 s | 67.5 s |
| Long-mode cuts added | – | 15 (7.09 s) |
| Voice guard drops/trims | – | 0 / 0 |

Per pause, as start–end in source seconds (length): cut, then what is left.

| Pause | Cut | Left |
|---|---|---|
| 2.27–3.19 (0.92) | 0.49 | 0.43 |
| 18.41–19.37 (0.96) | 0.64 | 0.32 |
| 24.03–24.65 (0.62) | 0.00 | 0.62 |
| 31.83–33.46 (1.63) | 1.32 | 0.31 |
| 40.50–41.10 (0.60) | 0.33 | 0.27 |
| 44.83–45.47 (0.64) | 0.36 | 0.28 |
| 48.55–49.34 (0.79) | 0.59 | 0.20 |
| 51.07–51.86 (0.79) | 0.50 | 0.29 |
| 67.22–68.19 (0.97) | 0.67 | 0.30 |
| 69.68–71.30 (1.62) | 1.35 | 0.27 |
| 72.24–73.66 (1.42) | 1.11 | 0.31 |

The 24.03 pause stays on purpose. A short span merges into it and leaves the word "çökmek." with no
audible core, so the whole stamp is protected. Tight mode also cuts 0.00 s there.

## Word check

The before and after audio were transcribed with whisper.cpp large-v3-turbo (`-l tr`).

- A baseline (source with the prod cuts only) transcribes identically to the prod output: 136 words.
- Baseline and after differ twice:
  - "10 bin" became "10.000", which is number formatting.
  - "Sizce" became "Size" at about 30.5 s. whisper-1 itself stamped this word as "Size". The nearest
    added cut ends 0.12 s before the word's onset (-42 dB at 30.56 s, room at -55 dB), and the word's
    samples are bit-identical.
- No added cut lies within 0.3 s of either word.
- No listening check was done. A device render with the flag on is owed (KRI-1270).

## Tests

- `tests/tasks/test_phone_long_pauses.py`:
  - Long mode adds only cuts that tight mode also makes, and leaves pauses under 0.6 s alone (on a take
    with contiguous stamps).
  - The tight report shape is unchanged.
  - With the flag off, the plan object is unchanged.
  - A tight ask stays on the tight path.
  - A cutter fault keeps the consented plan.
  - The worker stores the `long_pauses` receipt and updates the public counts.
- Neighbours: the tight-pause, silence-cut, receipt and KRI-1235 render-promise suites, plus the mapped
  reliability-gate tests. Postgres-backed tests are skipped locally.
