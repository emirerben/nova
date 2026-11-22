# KRI-1562: separate the voice from room sound?

**Real quiet and noisy takes: PENDING.** Every number below comes from synthetic stand-ins. It is not the real decision.

The question is whether a song under a Talking edit, ducked under her speech, still covers her voice. If it does on a real take, or if creators keep asking to remove room sound, the next step is a separate ticket for voice separation. This file records the measurement that decides it.

## Run it on the real takes

1. Put one quiet and one noisy talking clip (iPhone video, 20 to 40 s, Turkish or English) in `.dev/kri-1562-takes/quiet/` and `.dev/kri-1562-takes/noisy/`. That folder is gitignored.
2. Run the check with a real catalog song:

	```bash
	scripts/dev/kri1562-listening-check.sh --takes .dev/kri-1562-takes --song /path/to/catalog-song.m4a
	```

3. Read `.dev/kri-1562-listening/<run-id>/report.md`. Copy its numbers into the results table below. Copy numbers only, never transcript text.
4. Listen to the `ducked` export against the `control` export that the report links.

The first run downloads whisper.cpp's multilingual `ggml-small.bin` (466 MiB) into `~/.cache/kria-listening/`, or into the path that `--model` or `KRIA_WHISPER_MODEL` names. The script refuses a file whose sha256 is not the pinned one. Without `--song`, the song is the synthetic stand-in described under Samples.

## Method

`scripts/dev/kri1562_listening_check.py prepare` transcribes each take with whisper.cpp (`ggml-small`, language detected once on the take, then pinned). It builds the speech windows with the server's `speech_spans_from_words` and the bed with the server's `music_bed_from_treatment`, at the treatment defaults (bed at -18 dB, -12 dB under speech). It writes five recipes per take:

| variant | what it is |
| --- | --- |
| `nomusic` | `compile_phone_subtitled_plan` with no song. The voice reference. |
| `ducked` | The same compiler with the song and the windows. What production emits. |
| `control` | The same compiler with the song and `speech_source_windows={}`, so no windows. The duck turned off. |
| `ref` | `control` with `original_volume` 0. Harness only. The song alone, unducked. |
| `masker` | `control` with the bed clips at volume 1, the song at its mastered level. Harness only. The word check must flag it. |

`prepare` fails if `ducked` has no windows or no `speechDuck` capability, or if `control` differs from `ducked` in anything but the windows. It also fails when more than 5% of the take's words have zero length. That pattern appeared when whisper's alignment collapsed and a whole sentence fell outside every window.

`AudioParityFixtureTests` exports every recipe through `AVFoundationLocalExporter`, the production exporter, under `swift test` on macOS.

`measure` decodes the exports at 48 kHz. Per 20 ms frame (10 ms hop), the song's gain is the projection of (variant minus `nomusic`) on `ref`. The projection drops the voice, the room and the song's own loudness changes. It also drops most of the AAC coding error of the voice. A plain difference reads 0.4 dB high under speech on the quiet take and 1.6 dB high on the noisy one, because that error sits 10 dB (quiet) and 3.6 dB (noisy) under the ducked song. Speech is scored inside the recipe's windows. Pauses are gaps over 1 s between windows, scored from 0.35 s after a window to 0.15 s before the next one. The 0.5 s bed fades are excluded.

Word errors are the edit distance between the words whisper hears in an export and in `nomusic`, after case folding (every Turkish or English i folds to `i`) and punctuation removal. The floor is the most errors whisper makes when nothing that matters changes: the take before the exporter, and `nomusic` 1 dB quieter or louder.

## Criteria

| criterion | passes when |
| --- | --- |
| duck | The `ducked` song is at least 9 dB under its pause level while she speaks. |
| recovery | After every window followed by a pause over 1 s, the song is back within 1 dB of its pause level within 0.5 s. |
| control caught | The `control` export fails the duck criterion. If it passed, the check could not see a missing duck. |
| voice intelligible | The `ducked` word errors are at most 2 percentage points over the floor. If not, the report flags "voice masked, consider separation". |
| masking caught | The `masker` word errors are more than 2 percentage points over the floor. If not, the word check could not see masking with this song. |

The ticket's wording is "within 2 points of the no-music reference". Read literally, the reference scores 0 against itself, so the bar is 2% word error. The report prints that literal reading too, but the flag uses the floor. On the noisy stand-in, `nomusic` played 1 dB quieter already differs from `nomusic` by 3 words (3.4%), so the literal bar would call a volume change "masked".

## Results

Synthetic run `20261121-115312-synthetic`. A second full run, `20261121-115515-synthetic`, gave the same numbers in every row but the masker's word errors (47 instead of 38 on the quiet take, 54 instead of 53 on the noisy one).

| take | variant | gain in speech | gain in pauses | duck dB | worst recovery s | voice to music dB (speech band) | mix LUFS | word errors / words (floor) |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| synthetic quiet | ducked | 0.2508 | 1.0000 | 12.01 | 0.31 | 24.12 (23.89) | -19.9 | 1 / 89 (0) |
| synthetic quiet | control | 0.9976 | 1.0000 | 0.02 | n/a | 12.12 (11.90) | -19.8 | 4 / 89 (0) |
| synthetic quiet | masker | 7.9273 | 7.9460 | 0.02 | n/a | -5.88 (-6.11) | -13.3 | 38 / 89 (0) |
| synthetic noisy | ducked | 0.2504 | 0.9958 | 11.99 | 0.32 | 25.15 (25.45) | -19.6 | 4 / 88 (6) |
| synthetic noisy | control | 0.9953 | 0.9956 | 0.00 | n/a | 13.15 (13.46) | -19.4 | 9 / 88 (6) |
| synthetic noisy | masker | 7.9155 | 7.9260 | 0.01 | n/a | -4.86 (-4.54) | -13.0 | 53 / 88 (6) |
| real quiet | ducked, control, masker | PENDING | | | | | | |
| real noisy | ducked, control, masker | PENDING | | | | | | |

| take | duck | recovery | control caught | voice intelligible | literal 2% reading | masking caught |
| --- | --- | --- | --- | --- | --- | --- |
| synthetic quiet | pass | pass | pass | pass (1.1 points over the floor) | pass (1.1%) | pass (42.7 points) |
| synthetic noisy | pass | pass | pass | pass (2.3 points under the floor) | fail (4.5%) | pass (53.4 points) |
| real quiet | PENDING | | | | | |
| real noisy | PENDING | | | | | |

Voice to music is the take's whole sound (voice and room) against the song inside the windows.

### What limits each number

- **Duck, 12.0 dB.** The treatment's -12 dB sets it. The bed's `speech_duck_gain` is 0.2512, and the export measures 0.2504 to 0.2508.
- **Recovery, 0.30 to 0.32 s.** The engine's linear 0.35 s release in `AudioDuckEnvelope.speech` sets it. That ramp reaches -1 dB at 0.30 s, and the 10 ms frame step adds up to 0.01 s.
- **Control, 0.00 to 0.02 dB.** With no windows the song never moves, so the check fails it as it should.
- **Masker gain, 7.93.** Volume 1 undoes the bed's -18 dB, and 10^(18/20) is 7.94.
- **Gain just under 1 on the noisy take.** The control reads 0.995 (-0.04 dB). Under a loud voice and room, the AAC encoder spends fewer bits on the song. The quiet take reads 0.998 in speech and 1.000 in pauses.
- **Clock.** Each export lines up with the source take at 0 ms offset, and the exports line up with each other at 0 samples.
- **Voice loudness.** The voice is -23.0 LUFS in the mono source and -20.0 LUFS in the export. The export is dual mono, and BS.1770 adds 3 LU for the second channel. The exporter does not change the voice.
- **Windows.** Against the clean voice, the windows start 0.05 to 0.35 s early and end 0.07 to 0.24 s early. The song is more than 3 dB over its ducked level for 0.50 s (quiet) and 0.85 s (noisy) of 24.6 s of speech, at sentence ends.
- **Run to run.** The source takes and the song are bit-identical across the two runs, but the native exports are not. Their audio differs by about 60 dB under the signal. Whisper turns that into different words only at the masker's level, where it sits on the edge of hearing anything.
- **Word errors.** The duck is what keeps the words. Without it, `control` is 4.5 points over the floor on the quiet take and 3.4 on the noisy one, both over the 2 point bar. The noisy floor of 6 words comes from whisper itself. Between the take before the exporter and `nomusic`, it writes "Ok" or "Okay", "I'll" or "I will" (two errors), and "the" or "their", and guesses differently at two words it cannot hear in the babble. With the median floor (3 words) instead of the largest, the verdicts are the same.

## Samples

These samples are not committed. `--synthetic` builds them again on any Mac.

| sample | duration | how it was made |
| --- | --- | --- |
| synthetic quiet | 35.0 s | Five macOS `say` voices read five different sentences. Daniel, Karen, Moira and Samantha are the voices of the repo's `tts_*.m4a` fixtures, and Tessa is the fifth. Silence of 1.0, 1.6, 1.5, 2.0, 1.2 and 1.5 s sits before, between and after them. The voice is set to -23 LUFS, mono, under a solid 1080x1920 picture. |
| synthetic noisy | 35.0 s | The quiet take plus `crowd_03.m4a` (babble with no voice of its own), looped, at 6 dB speech-to-noise. Written as a 1920x1080 `.mov` with a -90 degree rotation, like an iPhone portrait clip. |
| song | 120 s | `music_02.m4a` (a synthesized 3 s drum and bass loop) repeated, plus an equally loud speech-shaped noise layer (200 to 5000 Hz, falling above 1 kHz, fixed seed) standing in for vocals and mid-range instruments. The mix is set to -14 LUFS. Its speech band (300 to 4000 Hz) sits 3.4 dB under its full level. |
| real quiet | PENDING | Supplied by the owner. |
| real noisy | PENDING | Supplied by the owner. |

The stand-ins do not use the repo's `tts_*.m4a` files directly. All twelve say one sentence, and whisper's alignment mapped one copy of it onto the next copy, so a whole sentence fell outside every window. Real speech does not repeat like that. The drum loop alone is not the song either, because its speech band sits 21.7 dB under its full level and it could never mask a voice.

## Decision

- **Synthetic stand-ins.** Separate voice from room sound: no. The duck works as specified. With it, the song adds at most 1 word over the floor. Without it, the same song costs 3 (quiet) and 5 (noisy) words more. This rests on synthetic samples only.
- **Real takes.** PENDING. If the real run flags "voice masked" on either take, open a separate voice-separation ticket and keep its samples out of git. This check does not measure how often creators ask for room sound to be removed, so that half of the question needs the request data.

## What the numbers do not prove

- **Masking by a real song.** The stand-in song is stationary noise over a drum loop. A real song has vocals with words of their own, which whisper may write into her transcript, and its level moves. Only a run with `--song` and a catalog song answers the masking question. This check did not measure a catalog song's loudness. The bed follows the song's own level, so a louder master sits louder under her voice.
- **Real voices and rooms.** TTS has no breath, no mic gain riding, no overlapping talk and no real room. The babble is synthesized and loops every 3 s.
- **Production's windows.** Production builds windows from whisper-1 words. This check uses whisper.cpp `small` with DTW token times, so production's windows can be better or worse than the ones measured here.
- **The phone itself.** The export runs the production exporter under `swift test` on macOS, not on an iPhone. The hardware encoder and listening on the speaker and on headphones stay human checks.
- **Turkish.** Both synthetic takes are English. Turkish takes go through the same code, but no Turkish number is in this file yet.
- **Sound effects.** No variant has sound effects. The ticket's "sound effects, music and voice together" line needs its own check.
- **Intelligibility.** Whisper's word errors stand in for a listener. With 88 words, one word is 1.1 points, so the 2 point bar allows one extra word.
