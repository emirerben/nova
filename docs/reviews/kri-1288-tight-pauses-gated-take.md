# KRI-1288: tight pauses leave 5 pauses over 0.35 s on a noise-gated take

## Request

"Cut out all my pauses and mistakes so it's tight." (Q1 run 2, job 72cf0827, `Talking take.mp4`, 48.8 s.)
Kria promised every pause about 0.3 s; 5 stayed over 0.35 s (longest 0.64 s, source 6.76-7.40 s).

## Root cause

The take has digital silence between sentences (frame levels of -120 dBFS; 2.3-3.0 s and 44.6-45.2 s read -120).
`quiet_floor_db` (5th percentile) therefore returned -120 dBFS, the "loud" bar (+15 dB) sat at -105 dBFS, and
the gate's own fade tails (-62 dBFS after "about.", -69 dBFS before "Number") counted as sound. The pause edges
were protected, the 0.64 s pause left a 0.11 s piece (< `TIGHT_MIN_CUT_S`) and got no cut. The voice guard used the
same -120 floor and dropped a further candidate (8.92-9.09 s).

## Fix

`tight_pause_cut.TIGHT_MIN_FLOOR_DBFS = -70`: the loud/soft bars and the voice guard (new optional
`min_floor_db`, default `None` = unchanged for every other caller) are measured from a floor no lower than that.
The noisy-take refusal still uses the raw floor.

## Offline replay (no paid calls)

`apply_tight_pauses` on the real source audio, with the job's saved `silence_cut.removed` as the plan and the
job's `overlay_transcript` as stamps. The saved plan already holds the prod run's tight cuts, so the "before" is
already partly tightened and the gain is understated.

| Measure | Before | After |
|---|---|---|
| Added cuts | 0 | 7 (0.64 s) |
| Pauses over 0.35 s | 5 | 3 |
| Longest kept pause | 0.64 s | 0.56 s |
| Voice guard drops | 1 (candidate) | 0 |

The 6.76-7.40 s pause is cut 6.91-7.27 s, leaving about 0.28 s.

Left over: a 0.43 s pause (15.89-16.32 s) with a faint sound inside, a 0.37 s pause (26.58 s), and the take's tail
(48.25 s). The receipt still reports them. A "why this pause was kept" line in the receipt is not part of this change.

## Not done

No listening check and no device render. Existing suites pass: tight-pause, long-pause, receipt, voice-guard (83).
