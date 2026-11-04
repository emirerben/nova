# audio_quality calibration

signal version 1; thresholds from `app/services/clip_signals/audio_quality.py` (FITTED).

## Honest scope

Every clip here is synthetic (numpy synthesis, encoded to AAC with ffmpeg): harmonic voiced speech, gusty low-passed noise for wind, summed continuous voices plus diffuse noise for crowd, kick/sub-bass/hat loops for bass music, hard-clipped speech, and silence. The fit set (30 committed clips) and the held-out set (60 clips, different seeds, wider parameter ranges, 2.5-6 s, 44.1/48 kHz, 32-96 kbps) come from the SAME generator family, so a good number here means the measurement separates the failure modes we thought of, not that it works on real phone audio. Real wind hits a phone mic differently (mic-specific noise reduction, AGC pumping), and real crowds and real music are far more varied. Treat the mute-clip gate below as necessary, not sufficient: re-run with `--real-dir` on labeled real clips before enabling `kria_suggestion_mute_clip_enabled` for creators.

## Mute-clip gate (precision >= 0.9)

- held-out precision: **1.00** (recall 0.96) -> **PASS**
- fit-set precision: 1.00 (recall 1.00); the fit set was seen during fitting
- bass-heavy music flagged noisy on held-out: 0/10
- held-out false positives: none
- held-out misses: ['wind_09.m4a']
- clips with no signal (absent): none

## How to read this

- Development used the fit set only. The held-out set was scored twice: once, then again after the periodicity measure was changed to autocorrelation peaks and a saturated-808 music variant was added; nothing was tuned on it beyond that.
- `noisy` (wind or crowd) is what the mute card may act on; the separate wind / crowd rows only say which evidence line a card would show. A traffic-like rumble can read as wind and is still a correct mute.
- `noisy` does not know whether the clip's speech matters. Muting a noisy clip also mutes its voice; the mute_clip kind has to decide that from understanding (speech present, voiceover or caption plans).
- The `silence` row counts the `silent` flag only. The -58 dBFS hiss clip is audible room tone by definition (peak above -55 dBFS), so it reads as not silent and not noisy.
- Sign priors (`WIND_SIGNS`, `CROWD_SIGNS`) stop the fit from using a feature against its physical meaning. A feature the data wants backwards gets weight 0 (see the FITTED block).

## Held-out set (60 clips)

| class (flag) | TP | FP | FN | precision | recall |
| --- | --- | --- | --- | --- | --- |
| wind | 11 | 2 | 1 | 0.85 | 0.92 |
| crowd | 11 | 0 | 1 | 1.00 | 0.92 |
| clipped | 6 | 0 | 0 | 1.00 | 1.00 |
| silence | 6 | 0 | 0 | 1.00 | 1.00 |
| mute (wind|crowd) | 23 | 0 | 1 | 1.00 | 0.96 |

| true label | n | noisy | wind | crowd | clipped | silent |
| --- | --- | --- | --- | --- | --- | --- |
| clean | 12 | 0 | 0 | 0 | 0 | 0 |
| wind | 12 | 11 | 11 | 0 | 0 | 0 |
| crowd | 12 | 12 | 2 | 11 | 0 | 0 |
| music | 10 | 0 | 0 | 0 | 0 | 0 |
| clipped | 6 | 0 | 0 | 0 | 6 | 0 |
| silence | 6 | 0 | 0 | 0 | 0 | 6 |

## Fit set (30 committed clips; optimistic by construction)

| class (flag) | TP | FP | FN | precision | recall |
| --- | --- | --- | --- | --- | --- |
| wind | 6 | 1 | 0 | 0.86 | 1.00 |
| crowd | 6 | 0 | 0 | 1.00 | 1.00 |
| clipped | 3 | 0 | 0 | 1.00 | 1.00 |
| silence | 3 | 0 | 1 | 1.00 | 0.75 |
| mute (wind|crowd) | 12 | 0 | 0 | 1.00 | 1.00 |

| true label | n | noisy | wind | crowd | clipped | silent |
| --- | --- | --- | --- | --- | --- | --- |
| clean | 6 | 0 | 0 | 0 | 0 | 0 |
| wind | 6 | 6 | 6 | 0 | 0 | 0 |
| crowd | 6 | 6 | 1 | 6 | 0 | 0 |
| music | 5 | 0 | 0 | 0 | 0 | 0 |
| clipped | 3 | 0 | 0 | 0 | 3 | 0 |
| silence | 4 | 0 | 0 | 0 | 0 | 3 |

## Detection by SNR (speech minus noise, held-out generator, 6 clips each)

Where the mute card stops firing as the noise gets quieter than the voice. A quiet gust under loud speech is deliberately not flagged.

| SNR dB (speech minus noise) | wind recall | crowd recall |
| --- | --- | --- |
| +12 | 3/6 | 6/6 |
| +8 | 4/6 | 6/6 |
| +4 | 4/6 | 6/6 |
| +0 | 6/6 | 6/6 |
| -4 | 6/6 | 6/6 |
| -8 | 6/6 | 6/6 |

## Real labeled clips

Not run. Put clips and a `labels.json` (`{"file.m4a": "wind|crowd|clean|music|clipped|silence"}`) in a folder and run `clip_signal_calibration.py report --real-dir DIR`; `fit --include-real --real-dir DIR` refits on them.
