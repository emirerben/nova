# Green-screen backgrounds: never show the same picture twice — incident and decision

**Ask (co-founder, 2026-11-04, on the S4 "fb 1" export):** "Aynı resmi iki kere göstermediği an 10/10" — the
result is right once no background picture is shown twice.

**Incident:** S4 run "fb 1" (prod 2026-11-04, job `c39a3210`): 19 background windows from 8 pictures, every
picture shown 2–3 times (11 repeats). An offline replay of `phone_background_sequence.lay_out` with the job's
stored sequence, word timings and speech cut reproduces all 19 windows to the hundredth of a second, so the
loop is the layout's, not the phone's.

**Why it looped:** two deliberate rules in `phone_background_sequence.py`:
- on the word (every entry phrased): once every visual was shown, they took turns every `cadence_s` to the take
  end — contract row S51 (PR #2091, Green 3), written so "changing every 3–5 seconds" stayed true;
- rotation (an entry without a phrase): the unanchored entries cycled `cadence_s` each until the take ended (S39).
In fb 1 the planner saw only the first ~30 s of speech (PR #2100), so all 8 mentions sat before 29 s and the
remaining 44 s were tail cycling.

**The clash and the decision (2026-11-05):** "change every 3–5 s" and "never repeat" cannot both hold when
pictures × 5 s < video length. Options considered: a plain colour between photos (rejected: "araya dümdüz renk
girmesi saçma"), asking the creator (rejected for the same reason), keeping S51 (rejected: repeats). **Decided
(Yasin, approved by Emir): photos stay in a row, each one stays until the next photo is mentioned — a longer scene
is fine — and the same photo never comes up twice.** No question to the creator, nothing plain in between.

The edge rules are rows S57–S63 of `docs/reviews/speaker-background-windows-failure-modes.md`.

| # | Failure | Decided guard |
|---|---|---|
| N1 | After the last mention the shown pictures take turns again (fb 1: 44 s of loop). | The last window holds to the take end (S57, replaces S51). |
| N2 | Rotation (no phrases) cycles a short list over a long take. | Each picture once; the cadence stretches to `max(cadence_s, take / pictures, take / 40)` (S57, replaces S39's cycling). |
| N3 | The planner lists the same picture twice. | The first-heard entry keeps its window; the other gets a `duplicate_media` note (S59). |
| N4 | The fill (usually entry 0) shows in a gap and repeats a picture. | When the fill is one of the pictures, the window before a gap holds through it (S58, S60). |
| N5 | A picture is held far longer than the asked pace and the creator is not told. | The receipt never names a pace (S63); remaining gap: "Yapıldı" beside the creator's own "every 3–5 s" wording. |
| N6 | A video window longer than the clip makes the clip loop. | Open: the phone loops a background video (`loop: true`); not changed here. |
| N7 | Stored strategies re-rendered later (editor Save, retake) change look. | They follow the new rule on re-render (no flag): the decision applies to every edit. |
| N8 | Two different uploads of the same photo. | Out of scope (no content hashing). |

Regression tests: `src/apps/api/tests/tasks/test_background_no_repeat.py` (invented Turkish take with fb 1's
timing shape; the real transcript stays out of git) plus the flipped S39/S51/S54 tests.
