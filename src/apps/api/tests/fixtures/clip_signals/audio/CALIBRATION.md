# audio_quality calibration

signal version 2; thresholds from `app/services/clip_signals/audio_quality.py` (FITTED).

## Honest scope

Most clips here are synthetic (numpy synthesis, encoded to AAC with ffmpeg): harmonic voiced speech, gusty low-passed noise for wind, summed continuous voices plus diffuse noise for crowd, kick/sub-bass/hat loops for bass music, hard-clipped speech, and silence. The clean class also includes text-to-speech voices (macOS `say`, not from our generator) rendered at several gains, in both sets, so the fit cannot learn that loud means noisy. The fit set (48 committed clips) and the held-out set (80 clips, different seeds and TTS voices/text/gains, wider parameter ranges, 2.5-6 s, 44.1/48 kHz, 32-96 kbps) come from the SAME synthetic family apart from the TTS clips, so a good number here means the measurement separates the failure modes we thought of, not that it works on real phone audio. Real wind hits a phone mic differently (mic-specific noise reduction, AGC pumping), and real crowds and real music are far more varied. Treat the mute-clip gate below as necessary, not sufficient: re-run with `--real-dir` on labeled real clips before enabling `kria_suggestion_mute_clip_enabled` for creators.

## Mute-clip gate (precision >= 0.9)

- held-out precision: **0.94** (recall 0.71) -> **PASS**
- fit-set precision: 1.00 (recall 0.92); the fit set was seen during fitting
- bass-heavy music flagged noisy on held-out: 0/10
- held-out false positives: ['clean_06.m4a']
- held-out misses: ['crowd_02.m4a', 'crowd_05.m4a', 'crowd_06.m4a', 'crowd_07.m4a', 'crowd_10.m4a', 'crowd_11.m4a', 'wind_09.m4a']
- clips with no signal (absent): none

## How to read this

- Development used the fit set only, but the held-out set is not untouched: it was scored more than once while the measurement changed (autocorrelation-peak periodicity, a saturated-808 music variant, then level normalization after a reviewer found that loudness alone flipped clean speech to noisy). Read the precision as an optimistic bound, and treat the 0.9 gate as unproven until real labeled clips pass it.
- Loudness: every spectral feature is measured after normalizing the clip's loud level, so gain does not move a verdict (see the gain table). The fit step also sees clean clips from -40 to -12 dB.
- Thin margin: the precision above rests on 18 flagged held-out clips, so one more false positive moves it by about 6 points. Recall is the cost of removing the loudness shortcut: speech well above a crowd (about +8 dB and more) is mostly left alone, because without an absolute floor it looks like dense clean speech.
- `noisy` (wind or crowd) is what the mute card may act on; the separate wind / crowd rows only say which evidence line a card would show. A traffic-like rumble can read as wind and is still a correct mute.
- `noisy` does not know whether the clip's speech matters. Muting a noisy clip also mutes its voice; the mute_clip kind has to decide that from understanding (speech present, voiceover or caption plans).
- The `silence` row counts the `silent` flag only. The -58 dBFS hiss clip is audible room tone by definition (peak above -55 dBFS), so it reads as not silent and not noisy.
- Sign priors (`WIND_SIGNS`, `CROWD_SIGNS`) stop the fit from using a feature against its physical meaning. A feature the data wants backwards gets weight 0 (see the FITTED block).

## Held-out set (80 clips)

| class (flag) | TP | FP | FN | precision | recall |
| --- | --- | --- | --- | --- | --- |
| wind | 11 | 2 | 1 | 0.85 | 0.92 |
| crowd | 6 | 5 | 6 | 0.55 | 0.50 |
| clipped | 6 | 0 | 0 | 1.00 | 1.00 |
| silence | 6 | 0 | 0 | 1.00 | 1.00 |
| mute (wind|crowd) | 17 | 1 | 7 | 0.94 | 0.71 |

| true label | n | noisy | wind | crowd | clipped | silent |
| --- | --- | --- | --- | --- | --- | --- |
| clean | 34 | 1 | 1 | 0 | 0 | 0 |
| wind | 12 | 11 | 11 | 5 | 0 | 0 |
| crowd | 12 | 6 | 1 | 6 | 0 | 0 |
| music | 10 | 0 | 0 | 0 | 0 | 0 |
| clipped | 6 | 0 | 0 | 0 | 6 | 0 |
| silence | 6 | 0 | 0 | 0 | 0 | 6 |

## Fit set (48 committed clips; optimistic by construction)

| class (flag) | TP | FP | FN | precision | recall |
| --- | --- | --- | --- | --- | --- |
| wind | 6 | 0 | 0 | 1.00 | 1.00 |
| crowd | 5 | 2 | 1 | 0.71 | 0.83 |
| clipped | 3 | 0 | 0 | 1.00 | 1.00 |
| silence | 4 | 0 | 0 | 1.00 | 1.00 |
| mute (wind|crowd) | 11 | 0 | 1 | 1.00 | 0.92 |

| true label | n | noisy | wind | crowd | clipped | silent |
| --- | --- | --- | --- | --- | --- | --- |
| clean | 24 | 0 | 0 | 0 | 0 | 0 |
| wind | 6 | 6 | 6 | 2 | 0 | 0 |
| crowd | 6 | 5 | 0 | 5 | 0 | 0 |
| music | 5 | 0 | 0 | 0 | 0 | 0 |
| clipped | 3 | 0 | 0 | 0 | 3 | 0 |
| silence | 4 | 0 | 0 | 0 | 0 | 4 |

## Clean TTS speech by gain (held-out voices, text and gains)

A clean voice must stay unflagged at every recording level. Crowd/wind scores are against the fitted thresholds shown in the header row.

| gain dB | clips | flagged noisy | max wind score (>= 0.3865) | max crowd score (>= 0.4539) |
| --- | --- | --- | --- | --- |
| -18 | 4 | 0 | 0.05 | 0.08 |
| -6 | 4 | 0 | 0.05 | 0.08 |
| +3 | 4 | 0 | 0.05 | 0.08 |
| +9 | 4 | 0 | 0.05 | 0.08 |

## Detection by SNR (speech minus noise, held-out generator, 6 clips each)

Where the mute card stops firing as the noise gets quieter than the voice. A quiet gust under loud speech is deliberately not flagged.

| SNR dB (speech minus noise) | wind recall | crowd recall |
| --- | --- | --- |
| +12 | 0/6 | 0/6 |
| +8 | 3/6 | 5/6 |
| +4 | 3/6 | 5/6 |
| +0 | 6/6 | 5/6 |
| -4 | 6/6 | 5/6 |
| -8 | 6/6 | 6/6 |

## Real labeled clips

Not run. Put clips and a `labels.json` (`{"file.m4a": "wind|crowd|clean|music|clipped|silence"}`) in a folder and run `clip_signal_calibration.py report --real-dir DIR`; `fit --include-real --real-dir DIR` refits on them.
