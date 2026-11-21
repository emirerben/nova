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

The first run downloads whisper.cpp's multilingual `ggml-small.bin` (466 MiB) into `~/.cache/kria-listening/`. The script refuses a file whose sha256 is not the pinned one. Without `--song`, the song is the repo's synthesized drum loop, which cannot settle masking (see the last section).

## Method

`scripts/dev/kri1562_listening_check.py prepare` transcribes each take with whisper.cpp (`ggml-small`, language detected once on the take, then pinned). It builds the speech windows with the server's `speech_spans_from_words` and the bed with the server's `music_bed_from_treatment`, at the treatment defaults (bed at -18 dB, -12 dB under speech). It writes four recipes per take:

| variant | what it is |
| --- | --- |
| `nomusic` | `compile_phone_subtitled_plan` with no song. The voice reference. |
| `ducked` | The same compiler with the song and the windows. What production emits. |
| `control` | The same compiler with the song and `speech_source_windows={}`, so no windows. The duck turned off. |
| `ref` | `control` with `original_volume` 0. Harness only. The song alone, unducked. |

`prepare` fails if `ducked` has no windows or no `speechDuck` capability, or if `control` differs from `ducked` in anything but the windows.

`AudioParityFixtureTests` exports every recipe through `AVFoundationLocalExporter`, the production exporter, under `swift test` on macOS.

`measure` decodes the exports at 48 kHz. Per 20 ms frame (10 ms hop), the song's gain is the projection of (variant minus `nomusic`) on `ref`. The projection drops the voice, the room and the song's own loudness changes. It also drops most of the AAC coding error of the voice. A plain difference reads 0.4 dB high under speech on the synthetic takes, because that error sits about 10 dB under the ducked song. Speech is scored inside the recipe's windows. Pauses are gaps over 1 s between windows, scored from 0.35 s after a window to 0.15 s before the next one. The 0.5 s bed fades are excluded.

Word errors are the edit distance between the words whisper hears in an export and in `nomusic`, after case folding (every Turkish or English i folds to `i`) and punctuation removal. The floor is the most errors whisper makes when nothing that matters changes: the take before the exporter, and `nomusic` 1 dB quieter or louder.

## Criteria

| criterion | passes when |
| --- | --- |
| duck | The `ducked` song is at least 9 dB under its pause level while she speaks. |
| recovery | After every window followed by a pause over 1 s, the song is back within 1 dB of its pause level within 0.5 s. |
| control caught | The `control` export fails the duck criterion. If it passed, the check could not see a missing duck. |
| voice intelligible | The `ducked` word errors are at most 2 percentage points over the floor. If not, the report flags "voice masked, consider separation". |

## Results

Synthetic run `20261121-114017-synthetic`. An earlier full run, with a shorter loop of the same song, gave the same numbers.

| take | variant | gain in speech | gain in pauses | duck dB | worst recovery s | voice to music dB (speech band) | mix LUFS | word errors / words (floor) | criteria |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| synthetic quiet | ducked | 0.2508 | 1.0002 | 12.01 | 0.31 | 21.18 (39.26) | -20.3 | 0 / 71 (0) | duck, recovery, voice pass |
| synthetic quiet | control | 0.9993 | 1.0000 | 0.01 | n/a | 9.18 (27.70) | -20.2 | 0 / 71 (0) | caught |
| synthetic noisy | ducked | 0.2512 | 0.9995 | 12.00 | 0.32 | 22.31 (40.01) | -19.7 | 2 / 71 (2) | duck, recovery, voice pass |
| synthetic noisy | control | 0.9989 | 0.9999 | 0.01 | n/a | 10.32 (29.19) | -19.5 | 2 / 71 (2) | caught |
| real quiet | ducked | PENDING | | | | | | | |
| real quiet | control | PENDING | | | | | | | |
| real noisy | ducked | PENDING | | | | | | | |
| real noisy | control | PENDING | | | | | | | |

Voice to music is the take's whole sound (voice and room) against the song inside the windows.

### What limits each number

- **Duck, 12.0 dB.** The treatment's -12 dB sets it. The bed's `speech_duck_gain` is 0.2512, and the export measures 0.2508 and 0.2512.
- **Recovery, 0.30 to 0.32 s.** The engine's linear 0.35 s release in `AudioDuckEnvelope.speech` sets it. That ramp reaches -1 dB at 0.30 s, and the 10 ms frame step adds up to 0.01 s.
- **Control, 0.01 dB.** With no windows the song never moves, so the check fails it as it should.
- **Clock.** Each export lines up with the source take at 0 ms offset, and the exports line up with each other at 0 samples.
- **Voice loudness.** The voice is -23.0 LUFS in the mono source and -20.0 LUFS in the export. The export is dual mono, and BS.1770 adds 3 LU for the second channel. The exporter does not change the voice.
- **Windows.** Against the clean voice, the windows start 0.21 to 0.31 s early and end 0.07 to 0.24 s early. The song is more than 3 dB over its ducked level for 0.40 s (quiet) and 0.89 s (noisy) of 19.8 s of speech, at sentence ends.
- **Word errors.** `ducked` and `control` score the same on both takes, so the word check did not tell ducked from unducked here. The noisy take's 2 errors are its floor. The take before the exporter already differs from `nomusic` by those 2 words. Whisper writes "Ok" or "Okay", and it hears "street" as "spurs", "speech" or "stories" in the babble.

## Samples

These samples are not committed. `--synthetic` builds them again on any Mac.

| sample | duration | how it was made |
| --- | --- | --- |
| synthetic quiet | 28.8 s | Four macOS `say` voices (Daniel, Karen, Moira, Samantha, the voices of the repo's `tts_*.m4a` fixtures) read four different sentences. Silence of 1.0, 1.6, 1.5, 2.0 and 1.5 s sits before, between and after them. The voice is set to -23 LUFS, mono, under a solid 1080x1920 picture. |
| synthetic noisy | 28.8 s | The quiet take plus `crowd_03.m4a` (babble with no voice of its own), looped, at 6 dB speech-to-noise. Written as a 1920x1080 `.mov` with a -90 degree rotation, like an iPhone portrait clip. |
| song | 120 s | `music_02.m4a` (a synthesized 3 s drum and bass loop) repeated and set to -14 LUFS. |
| real quiet | PENDING | Supplied by the owner. |
| real noisy | PENDING | Supplied by the owner. |

The repo's `tts_*.m4a` fixtures are not used directly. All twelve say one sentence, and whisper's alignment mapped one copy of it onto the next copy, so a whole sentence fell outside every window. Real speech does not repeat like that.

## Decision

- **Synthetic stand-ins.** Separate voice from room sound: no. The duck works as specified, and the music did not add word errors over the floor. This rests on synthetic samples only.
- **Real takes.** PENDING. If the real run flags "voice masked" on either take, open a separate voice-separation ticket and keep its samples out of git. This check does not measure how often creators ask for room sound to be removed, so that half of the question needs the request data.

## What the numbers do not prove

- **Masking by a real song.** The synthetic song has its speech-band (300 to 4000 Hz) level 21.7 dB under its full level, so even unducked it sits 28 dB under the voice in that band. It cannot mask speech, and the word check could not fail on it. A real song with vocals and mid-range instruments can. Only a run with `--song` and a catalog song answers the masking question.
- **Real voices and rooms.** TTS has no breath, no mic gain riding, no overlapping talk and no real room. The babble is synthesized and loops every 3 s.
- **Production's windows.** Production builds windows from whisper-1 words. This check uses whisper.cpp `small` with DTW token times, so production's windows can be better or worse than the ones measured here.
- **The phone itself.** The export runs the production exporter under `swift test` on macOS, not on an iPhone. The hardware encoder and listening on the speaker and on headphones stay human checks.
- **Turkish.** Both synthetic takes are English. Turkish takes go through the same code, but no Turkish number is in this file yet.
- **Sound effects.** No variant has sound effects. The ticket's "sound effects, music and voice together" line needs its own check.
- **Intelligibility.** Whisper's word errors stand in for a listener. With 71 words, one word is 1.4 points, so a 2 point margin allows one extra error.
