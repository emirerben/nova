# Caption "bottom" position (asked-bottom captions ended at 0.70)

Cofounder's phone video showed captions about 62% down the 1920 px canvas although
"bottom" was chosen. `phone_text_style._candidates` used 0.70 as the BOTTOM EDGE of an
asked-bottom caption block, so a 1-2 line block centred at ~0.62-0.68. The compiler
already lifts a caption clear of the watermark, so the cap was redundant.

Fix: captions end at `CAPTION_BOTTOM_EDGE = 0.84` (default caption spot, centre ~0.80).
Other roles keep 0.70.

Measured with `resolve_phone_text_style` on the portrait canvas (box top/bottom, canvas fraction):

| asked position | before (bottom edge) | after |
| --- | --- | --- |
| bottom, 1 line | 0.70 (centre ~0.68) | 0.793-0.840 (centre 0.816) |
| bottom, 3 lines | 0.70 (centre ~0.62) | 0.749-0.840 (centre 0.794) |
| top / middle | unchanged | unchanged |

Open: very long text in `bottom_left` still falls to a lifted rung (box 0.54-0.72);
the unasked face-avoidance path (`choose_caption_y_frac`) was not changed or measured.

## Review follow-up: final compiled geometry (P2)

Through the real writer + compiler (`tests/pipeline/test_caption_bottom_frame.py`), a long
(118 chars) large (96 px) asked bottom_left caption ended at 1.017 of the height (off the
frame) and bottom / bottom_right at 0.948: the watermark pass re-breaks the cue and grows it.
Fix: an asked caption position carries `PhoneCaptionLook.frame_budget_frac` (0.88); the
watermark rewrap never grows the block past it, and a cue the pass leaves alone is lifted
(never shrunk) to fit. Unasked captions have no budget and compile exactly as before.

| case (asked) | final ink bottom before | after |
| --- | --- | --- |
| bottom_left, long, large | 1.017 | 0.879 |
| bottom / bottom_right, long, large | 0.948 | 0.880 |
| any, short | 0.840 | 0.840 |

Size is kept (96 px) in every case. Red control: with the compiler change stashed, 3 of the
15 new cases fail.
