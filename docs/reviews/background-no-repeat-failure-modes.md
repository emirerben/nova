# Green-screen backgrounds: never show the same picture twice — failure modes (draft, before code)

**Ask (co-founder, 2026-11-04, on the S4 "fb 1" export):** "Aynı resmi iki kere göstermediği an 10/10" — the
result is right once no background picture is shown twice.

**Incident:** S4 run "fb 1" (prod 2026-11-04, job `c39a3210`): 19 background windows from 8 pictures, every
picture shown 2–3 times (11 repeats). An offline replay of `phone_background_sequence.lay_out` with the job's
stored sequence, word timings and speech cut reproduces all 19 windows to the hundredth of a second, so the
loop is the layout's, not the phone's.

**Why it loops today:** two rules in `phone_background_sequence.py`, both deliberate:
- on the word (every entry phrased): "once every visual was shown, those without `hold_s` take turns every
  `cadence_s` … to the take end" — contract row **S51** (PR #2091, Green 3), written because holding one picture
  for the rest of the take broke "changing every 3–5 seconds";
- rotation (an entry without a phrase): the unanchored entries cycle `cadence_s` each until the take ends (S39).
In fb 1 the planner saw only the first ~30 s of speech (PR #2100), so all 8 mentions sat before 29 s and the
remaining 44 s were tail cycling.

**The clash:** "change every 3–5 s" and "never repeat" cannot both hold when pictures × 5 s < video length
(fb 1: 9 pictures, 73 s → 15–25 changes needed). One of the two has to give, and the creator should hear which.

| # | Failure | Proposed guard (needs a product decision before it is built) |
|---|---|---|
| N1 | After the last mention the shown pictures take turns again (fb 1: 44 s of loop). | The tail extends the last window to the take end instead of cycling (replaces S51). |
| N2 | Rotation (no phrases) cycles a short list over a long take. | Each picture shown once: the cadence stretches to `max(cadence_s, take_s / pictures)`; the existing `cadence_stretched` note fires. |
| N3 | The planner lists the same picture twice (two entries, one `media_id`). | Only the first entry gets a window; a note says it was merged. |
| N4 | The fill (the strategy's own `media_id`, usually entry 0) shows in a gap and repeats entry 0. | With a sequence, a gap is covered by the window before it, not the fill. |
| N5 | A picture is held far longer than the asked pace and the creator is not told. | Receipt + reply say "N pictures for M s: each stays about X s" before rendering (needs the brief check; may be a follow-up). |
| N6 | A video window longer than the clip makes the clip loop (looks like a repeat). | Open question: freeze on the last frame vs loop; check what the phone does today. |
| N7 | Stored strategies re-rendered later (editor Save, retake) change look without being asked. | Same layout on re-render; decide whether a flag gates the new rule. |
| N8 | Two different uploads of the same photo. | Out of scope (no content hashing); noted. |

Regression red test: `src/apps/api/tests/tasks/test_background_no_repeat.py` (invented Turkish take with fb 1's
timing shape; the real transcript stays out of git).
