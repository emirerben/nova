# KRI-1236: "Make it without these" — failure modes and expected outcomes

Parent: KRI-1234 (Qendresa Q1, prod thread `b931bc4e`, 2026-11-06 20:48-20:54 UTC, Fly main
`11310a4ea`, TestFlight 1910, protocol 42). Written before the fix; the replay is
`src/apps/api/tests/kria/test_kri1236_simpler_leaves_out_only_unsupported_postgres.py` over the
prod record `src/apps/api/tests/fixtures/kri1236_qendresa_q1_simpler.json` (read-only admin GETs).

## What prod recorded

- The prompt held eleven asks (r1-r11). Readiness held eight: r4 (screenshots / recordings in the
  top or bottom part) `unsupported`; r1 (tight pauses), r5-r9 (the "1. Spec-kit" labels) and r10
  (the "comment REPO" ending with an arrow) `needs_choice` render promises.
- The first card named nothing: the eight-ask "leaves out" sentence was 607 characters, over the
  600-character cap, so the card recorded no ids and the tap was an unnamed (legacy) consent. The
  held reply was cut at 1200 characters after r8 ("-…"): r9 and r10 were never shown.
- Tap 1: brief v2 kept r2, r3, r10, r11. Every removed row carries `superseded_by: creator`: the
  Main Creator's own `remove` updates, not the server's omission (`superseded_by: server`). Any
  model `remove` cancels the server's consent (KRI-1113 only waives a `change`).
- r10 survived because the model removed what the cut reply showed (and r9 with its siblings);
  readiness then held r10 (the arrow promise) and a second, named card offered it.
- Tap 2: the model removed r10 ("I've removed the closing arrow request"); the ending's words went
  with the arrow. The draft and the after-render review listed r2, r3, r11 as done and nothing as
  left out.

## Failure modes

| # | Failure | Expected outcome |
|---|---------|------------------|
| F1 | The tap removes asks the card never named (promises: pauses, labels, ending). | Only the card's ids are removed; with KRI-1235's verdicts the Q1 card names only r4. |
| F2 | The model's `remove` updates on the tap turn become the creator's removals. | On a pure "Make it without these" answer (tap or exact label/alias) the model's removals are dropped; the server applies the card's list (`superseded_by: server`). |
| F3 | A removed ask is missing from the draft reply or the after-render review. | "Left out as agreed: …" for each removed ask in the draft and in the review. |
| F4 | The card names a truncated list, or none (600-char cap). | The card names every ask it leaves out (cap 900, Q1's eight asks fit); a list too long to name whole gets no card, never an unnamed one. |
| F5 | The ending's words go with its arrow. | An ending held only because its arrow is missing keeps its words; only the arrow is left out (`arrow_left_out`), named as such in the card, draft and review. |
| F6 | A loop: the same card again, or a second card after the first. | One hold for Q1 with KRI-1235's verdicts. |
| F7 | A typed instruction that removes asks ("Drop the numbered labels") is mistaken for the tap. | It stays the creator's removal (`superseded_by: creator`). |
| F8 | Prod's own unnamed card still consents to a set it never showed. | Tapped after the fix it drops nothing and re-offers the current set by name. |
| F9 | Over-reach: asks Kria really cannot do but readiness calls `needs_choice` / `evidence_missing` (a named line that may stay, wind noise) can no longer be left out. | They stay on the card (KRI-901 and approval-readiness suites); promises are kept off it by KRI-1235, not by this card. |
| F10 | A hold after the tap reads "Here's the simpler version … Should I make it without these?". | It says what still blocks and offers no new card. |

## Limits

- Readiness verdict rows are replayed from prod's event 7 (and, for the done-when, with the
  needs_choice promises approvable as KRI-1235 makes them); the planner's output is prod's
  approved strategy and the model's brief updates as prod persisted them. No live model, device or
  render ran.
- The arrow-only omission changes what the phone CTA ending draws (no arrow); that export is not
  rendered here.
