# KRI-840 — verification (offline replay)

Evidence level: **offline replay** (deterministic fixtures through the real grounding,
compiler, worker and receipt code; no live model, no device export, no deployment).
Expected outcome: [acceptance.md](acceptance.md). Failure modes written before the fix:
[failure-modes.md](failure-modes.md).

## Red control

`tests/kria/test_kri840_fullscreen_beat.py` as first committed (before any fix) on main
`004f38182`: **29 failed, 9 passed**. The 9 are guards that must not change: legacy
strategy dump and hash, card starts on the word, corner beat stays a corner card, right
display is met, neutral ask gets no display verdict. The failures named the bug:

- the oblea and grill photos were drawn as 0.36-wide corner cards;
- a corner card got `met` for a full-screen ask (EN and TR);
- even-slot `fullscreen_images` got "I can't verify this one automatically yet" or
  "This draft has no pop-ins timed to your words".

## Result on the fix

```
cd src/apps/api && pytest tests/kria/test_kri840_fullscreen_beat.py -q
49 passed
```

A reviewer also checked the result independently. It drove the kit word timings through
`ground_phone_reaction_beats`, then `compile_phone_subtitled_plan`, and once through the
real phone Talking worker:

| Case | Full-screen photo (`subtitled-fullscreen`) | Corner card (`subtitled-overlays`) | Speaker track / audio | Captions over the photo |
|---|---|---|---|---|
| Don Julio T2, `hold_s` 1.2 | grill 5.000 s, 1.200 s, volume 0, cover-fill | chimichurri 8.000 s, 3.0 s, width 0.36 | `clip-0` 0–12.9 s, `original_volume` 1.0 | cue 4.2–6.1 s (worker: 5.0–8.0 s) |
| Café T2, no hold | oblea 5.640 s, 1.500 s, volume 0, cover-fill | Cotopaxi 2.550 s, 3.0 s, width 0.36 | `clip-0` 0–24.6 s, `original_volume` 1.0 | cue 5.54–9.38 s |

- `validate_phone_pilot_recipe`: OK for both.
- Worker lane receipt: `applied: [overlays]`, `dropped: []`.
- Beat receipt: the full-screen photo is recorded with `display_mode: "fullscreen"`; the
  corner card has no display mode (absent means corner).
- Device capabilities required: the same as the existing KRI-297 full-screen sequence
  recipe, so the iPhone needs nothing new.

### Receipts

| Plan | Ask | Result |
|---|---|---|
| Full-screen beat | Full screen (EN) | `met` |
| Full-screen beat | Full screen (TR) | `met` |
| Corner card | Full screen (EN) | `partial`: For "Argentinian", the photo shows as a small card in the corner, not full screen. |
| Corner card | Full screen (TR) | `partial`: "traditional Colombian dessert" için fotoğraf tam ekran değil, köşede küçük bir kart olarak çıkıyor. |
| Full-screen beat | Corner card | `partial` |
| Even-slot `fullscreen_images` only | Full screen at a word | `partial` (full screen, but spread evenly, not timed to the word) |

## Neighbouring checks

These all passed:

- reaction-beat receipts, KRI-667 regex ratchet, render-contract field matrix, wire contract;
- Main Creator agent, user-song and brief-extractor version pins;
- phone subtitled plan, reaction and overlay grounding;
- phone subtitled worker tasks, `nova_steps`;
- free `tests/evals -k main_creator` replay, including the hand-authored
  `kri840_fullscreen_reaction_beat.json` fixture.

The Voiceover prompt slice is byte-identical to main.

### After rebasing onto #1756 (Voiceover full screen, merged)

Main's Voiceover work already owned the `display_mode` field, the `allow_fullscreen`
grounding gate and the "fullscreen" slot; this fix layers on it (Talking repair downgrades
instead of dropping, the Talking worker passes a 1.5 s default hold, receipts compare the
ask's `display_mode`). One run of the KRI-840 file, the N2 Voiceover contract, grounding,
dispatch and narrated-recipe tests, the KRI-598 planner, KRI-839 spoken-word beats,
reaction-beat and marathon receipts, the KRI-667 ratchet, creator capabilities, render
contract, wire contract, prompt pins and `nova_steps`: **1399 passed**. Free
`tests/evals -k main_creator`: **69 passed**.

## Open gaps

- **No live model run.** Nothing here shows that the live Main Creator sets
  `display_mode` on the beat or records the `display_mode` brief fact. The replay fixture
  is hand-authored.
- **No iPhone export of either kit case.** A phone re-send is owed (KRI-829).
- **Editor changes go stale.** The render receipt is not refreshed when a creator later
  switches a beat card between full screen and corner in the editor. This was already
  true for timings.
- **Very short windows can vanish.** A full-screen window cut below 0.3 s (a word in the
  last 0.3 s, or a cleanup cut) is skipped by the compiler, but the receipt still says
  placed. This is rare.
