# KRI-1409: a room hum held a take join open (ylab, build 1977)

## Request and acceptance

Job `1f2de191-0ec7-4c2b-8c2b-bec4f8e1f327` (2026-11-13), five talking takes, German ask
"Schneide alle meine Pausen heraus, damit es straff wirkt." Clean up speech chosen.

Acceptance (from the ticket, independent of the cutter): every join between two takes follows
the same tight-pause target as a pause inside a take (`target_s 0.3`, `limit_s 0.35`), and no
spoken word is cut.

Recorded `take_join_gaps`: 0.299, 0.289, 0.454, **1.496 s** (after take 3). `take_join_trims` had
no row for take 3's end or take 4's start. The finished video measures a 1.642 s silence at
73.03-74.68 s (acceptance silence definition: highpass 90 Hz, -38 dB, 0.12 s).

## What the replay showed

The five analysis proxies were replayed through `apply_tight_pauses(trim_head=True,
trim_tail=True, min_cut_s=TIGHT_MIN_CUT_OVER_LIMIT_S)` (offline, local ffmpeg 8.1, word stamps
approximated from the acceptance silence spans; no media or transcript kept in git).

- The replay reproduces the recorded behaviour: take 3 keeps its whole tail, take 4 keeps its
  whole lead-in, take 3's head is trimmed 0.19 s (the recorded `start`/take 3 row).
- The silence is room tone: take 3 after its last word sits at -61 to -68 dBFS, take 4 before
  its first word at -64 to -68 dBFS; her speech peaks around -15 dBFS.
- The voice guard found "voice" there: periodic frames at -64.3 dBFS in take 3 (21.66-21.83 s,
  floor -68.7) and -64.2 / -66.7 dBFS in take 4 (0.16-0.29 s, 0.39-0.44 s, floor clamped -70).
  That is 3-6 dB over the floor, which clears the guard's 3 dB bar.
- The guard trimmed each edge cut back to the far side of that hum, and the short-fragment rule
  then dropped both cuts whole, so the join kept everything.
- The earlier guess ("background noise or a breath") was close; the backup join trimmer alone
  would not have helped, since a guarded backup trim hears the same hum.

## Change

Only for join-edge cuts (before the first kept word with `trim_head`, after the last with
`trim_tail`), the voice guard counts a periodic frame as voice when it is at least
`TIGHT_QUIET_ABOVE_FLOOR_DB` (15 dB) over the floor: the same bar the tight cutter already uses
for audible sound it protects. Pauses between words keep the guard's 3 dB bar. Words stay
protected by their stamps plus `PAD_S`, and any sound 15 dB over the room stays protected by the
cutter's loud runs.

## Result on the real takes

| | take 3 tail | take 4 lead-in | join |
| -- | -- | -- | -- |
| before (same replay, uncut takes) | 1.421 s | 0.576 s | 1.997 s |
| after | 0.146 s | 0.136 s | **0.282 s** |

Guard counts for cuts between words are unchanged (take 3: 1 dropped, take 4: 3 dropped).

## Tests

`tests/pipeline/test_tight_pause_cut_take_edges.py` (KRI-1409 section): a hum about 6 dB over
the room at both join edges is trimmed to the pad (failed before the change: 1.1 s lead kept);
the same hum between two words is still guarded (fails if the relief leaks to inner cuts:
checked by applying the 15 dB bar to every cut); a sound about 19 dB over the room after the
last word is kept.

## Not covered

- No new phone run of the ylab kit yet; the fix ships for every several-take tight render.
- The replay used approximate word stamps, not the job's whisper words or consented plans.
